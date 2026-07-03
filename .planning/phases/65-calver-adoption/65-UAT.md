---
status: complete
phase: 65-calver-adoption
source:
  - 65-01-SUMMARY.md
  - 65-02-SUMMARY.md
started: 2026-07-03
updated: 2026-07-03
driven_by: claude (operator-delegated; all deliverables deterministically verifiable, no human-perception items)
---

## Current Test

[testing complete]

## Tests

### 1. Cold Start Smoke Test (config still parses/validates)
expected: The files this phase touched with structural surface (ci.yml trigger glob, both compose comment blocks) still parse and validate from scratch — no YAML break, no CI-syntax break.
result: pass
evidence: `actionlint` PASSED on ci.yml + docker-publish.yml; `yamllint` PASSED on both compose files + ci.yml; `yaml.safe_load` parses all 3 edited YAML files clean. (Phase changes to the compose files are comment-only — no service/env/image line changed — so a full app-boot smoke test is not the relevant surface; config-validity is.)

### 2. CI release-tag trigger fires for a CalVer tag
expected: Pushing a bare CalVer release tag (e.g. `2026.7.0`) matches `ci.yml`'s `on: push: tags` glob and runs the publish pipeline; a wrong/legacy shape does not. This is the highest-consequence outcome (a wrong glob = silent no-publish).
result: pass
evidence: `ci.yml:13` = `tags: ["[0-9]+.[0-9]+.[0-9]+"]`; legacy `v*.*.*` count = 0. Glob-semantics emulation: `2026.7.0`→MATCH, `2026.12.10`→MATCH, `v2026.7.0`→reject, `2026.7`→reject, `2026.7.0-rc1`→reject, `main`→reject. Guarded by `test_ci_workflow_triggers_on_version_tags` (green, hardened to exact-shape in review).

### 3. Version source of truth reads CalVer
expected: `pyproject.toml` reports the CalVer version and `uv.lock` is in sync (not drifted / hand-edited).
result: pass
evidence: `pyproject.toml:7` = `version = "2026.7.0"`; `uv lock --check` → "Resolved 174 packages" clean (lock in sync).

### 4. Milestone↔version mapping is discoverable, history intact
expected: `MILESTONES.md` presents a `Milestone | Version | Date` table listing the historical `v1.0`..`v7.0` releases verbatim plus the new `2026.7.0` row; existing per-milestone detail sections are untouched.
result: pass
evidence: `MILESTONES.md:15` = `| Milestone | Version | Date |`; 10 table pipe-lines; all rows present — v1.0, v2.0, v3.0, v4.0, v5.0, v6.0, v7.0, 2026.7.0.

### 5. CalVer scheme + publish invariant documented for operators
expected: Docs state the `YYYY.MM.REVISION` scheme (no-leading-zero month, per-month zero-based REVISION) and the annotated-tag-PUSH-triggers-publish invariant with the delete-recreate recovery recipe.
result: pass
evidence: `YYYY.MM.REVISION` appears in docs/deployment.md (3×) + MILESTONES.md (1×); `deployment.md:352` documents bare tags/no-leading-zero month/per-month REVISION; `:354` states the push invariant + `git push --delete origin` recreate recipe.

### 6. Forward-looking examples read CalVer; historical labels preserved
expected: Every forward-looking pin/build example across docs + compose comments reads `2026.7.0`; no forward-looking `v`-prefixed release instruction remains in live source; historical/feature-era `vN.M` labels are left verbatim.
result: pass
evidence: `grep PHAZE_IMAGE_TAG=v[0-9]` over docs/ + both compose files → none (all read `2026.7.0`, e.g. deployment.md:262/407, cloud-burst.md:250). Historical labels preserved: deployment.md "Phaze v4.0" header + cloud-burst v5.0 feature labels intact. (Verified independently by 65-VERIFICATION.md + 65-REVIEW.md — remaining `v`-prefix hits are all `.planning/` history.)

### 7. Publish machinery + README preserved
expected: `docker-publish.yml`'s metadata-action is functionally unchanged (bare `type=semver` already parses `2026.7.0`); README.md is untouched (no version badge exists → no badge added/removed).
result: pass
evidence: `docker-publish.yml` diff vs base = exactly 1 comment line (`:v<version>`→`:<version>`), tags/metadata-action body untouched; `git diff README.md` → UNCHANGED.

### 8. Guard tests are permanent green CI gates
expected: The four CalVer guard tests run green and will catch a future regression (dropped glob, missing mapping table, undocumented scheme, weakened publish assertion).
result: pass
evidence: `uv run pytest tests/agents/deployment/test_agent_compose.py` → 13 passed. The 4 CalVer gates (`test_ci_workflow_triggers_on_version_tags`, `test_milestones_mapping_table_intact`, `test_calver_scheme_documented`, `test_docker_publish_workflow_tags_both_latest_and_version`) are permanent CI gates.

## Summary

total: 8
passed: 8
issues: 0
pending: 0
skipped: 0

## Gaps

[none]
