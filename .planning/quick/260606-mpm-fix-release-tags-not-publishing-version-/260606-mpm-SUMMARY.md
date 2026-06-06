---
phase: quick-260606-mpm
plan: 01
type: execute
subsystem: ci
tags: [ci, release, docker, ghcr, deployment]
requires: []
provides:
  - "ci.yml release-tag publish trigger (push.tags v*.*.*)"
  - "detect-changes tag-ref code-changed forcing"
  - "strengthened CI tag-pipeline guard test"
affects:
  - .github/workflows/ci.yml
  - tests/test_deployment/test_agent_compose.py
  - docs/deployment.md
  - .env.example.agent
key-files:
  created: []
  modified:
    - .github/workflows/ci.yml
    - tests/test_deployment/test_agent_compose.py
    - docs/deployment.md
    - .env.example.agent
decisions:
  - "Tag forcing implemented by extending the existing schedule/workflow_dispatch early-exit with a REF_TYPE==tag condition rather than adding new diff logic, so tag pushes exit before the base/before-sha diff path that has no meaningful base."
  - "Guard tests handle the PyYAML on:->True boolean-key gotcha by resolving triggers via data.get('on', data.get(True))."
requirements: [RELEASE-TAG-01]
metrics:
  tasks: 3
  files_changed: 4
  commits: 3
  completed: 2026-06-06
---

# Quick Task 260606-mpm: Fix Release Tags Not Publishing Version-Tagged Images Summary

Make a `git push` of a 3-part semver tag (`vX.Y.Z`) trigger the full CI pipeline and publish `ghcr.io/simplicityguy/phaze:vX.Y.Z` (+ `:X.Y`), locked in by a guard test that fails if either the tag trigger or the tag-ref `code-changed` forcing regresses.

## What Changed

### Task 1 — ci.yml tag trigger + tag-ref change detection (`b3f57a7`)
- Added `on.push.tags: ["v*.*.*"]` alongside the existing `on.push.branches: ["**"]` and untouched `on.pull_request.branches: ["**"]` triggers.
- Wired `REF_TYPE: ${{ github.ref_type }}` into the `detect-changes` `id: filter` step `env:` block.
- Extended the existing `schedule`/`workflow_dispatch` early-exit `if` to also match `[[ "${REF_TYPE}" == "tag" ]]`, forcing `code-changed=true` for tag refs and updating the echo to `⚙️ Scheduled/manual/tag build — running full pipeline`. The PR/branch/before-sha diff logic below is untouched — tag pushes exit early before reaching it.
- `docker-publish.yml` was intentionally left unchanged; its `docker/metadata-action` already emits `type=semver,pattern={{version}}`, `{{major}}.{{minor}}`, and `type=ref,event=tag`, which become reachable now that tags trigger the pipeline.

### Task 2 — strengthened guard test (`df05f93`)
- Added `CI_WORKFLOW_PATH` constant plus `_load_ci_workflow_triggers` (PyYAML `on:`→`True` boolean-key resolver) and `_ci_detect_changes_filter_step` helpers.
- `test_ci_workflow_triggers_on_version_tags`: asserts `on.push.tags` contains a 3-part `v*.*.*` glob AND that `on.push.branches` survives (branch-CI regression guard).
- `test_ci_detect_changes_forces_code_changed_on_tags`: asserts the filter step `env` wires a ref-type/ref variable AND the run script forces `code-changed=true` for tag refs (case-insensitive `tag`/`ref_type`/`refs/tags` token match).
- All pre-existing tests (including `test_docker_publish_workflow_tags_both_latest_and_version`) remain unchanged and pass.

### Task 3 — docs + env confirmation (`b811a9e`)
- `docs/deployment.md`: Build Pipeline "Tag strategy" section and "Pinning the agent image for production" section now state release tags MUST be 3-part `vX.Y.Z` and that `ci.yml` publishes on `push` of a `v*.*.*` tag. gsd-doc-writer line-1 marker preserved.
- `.env.example.agent`: tightened the `PHAZE_IMAGE_TAG` comment to require a 3-part `vX.Y.Z` pin and note the `v*.*.*` trigger. Default `PHAZE_IMAGE_TAG=latest` line unchanged.

## Verification

- `uv run pytest tests/test_deployment/test_agent_compose.py -q` → 7 passed.
- **Regression proof (key acceptance criterion):** temporarily removing the `tags:` trigger fails `test_ci_workflow_triggers_on_version_tags`; temporarily removing both the `REF_TYPE` env and the `tag` OR-condition fails `test_ci_detect_changes_forces_code_changed_on_tags`. ci.yml restored via `git checkout --` after each check; full suite green afterward. The tests are not false-confidence static-only coverage.
- `uv run ruff check .` → all checks passed.
- Per-commit pre-commit suite (actionlint, yamllint strict, check-jsonschema, ruff, ruff-format, mypy) passed on every commit. No `--no-verify` used.
- ci.yml YAML parses; `on.push.tags` includes `v*.*.*`; filter step wires `REF_TYPE` and forces `code-changed=true` on `tag`.

## Deviations from Plan

None — plan executed exactly as written. (mypy reports two pre-existing errors on the test file at the `import yaml` and original `_load_agent_compose` lines; these are out of scope per the SCOPE BOUNDARY rule and are not surfaced by the repo mypy hook, which excludes `tests/`. The one mypy error introduced by this work — a `dict[str, Any].get(True)` overload mismatch — was fixed inline by typing the trigger-loader parameter as `dict[Any, Any]`.)

## Known Stubs

None.

## Self-Check: PASSED

- FOUND: .github/workflows/ci.yml (modified, parses, tags + REF_TYPE present)
- FOUND: tests/test_deployment/test_agent_compose.py (2 new tests, 7 passed)
- FOUND: docs/deployment.md (3-part scheme documented, line-1 marker intact)
- FOUND: .env.example.agent (3-part pin note added)
- FOUND commit: b3f57a7 (ci tag trigger)
- FOUND commit: df05f93 (guard test)
- FOUND commit: b811a9e (docs)
