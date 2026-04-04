---
phase: 01-infrastructure-project-setup
plan: 03
subsystem: ci-cd
tags: [github-actions, ci, codecov, security, code-quality]

dependency_graph:
  requires: [01-01]
  provides: [ci-pipeline, codecov-config]
  affects: [all-future-prs]

tech_stack:
  added: [github-actions, codecov, semgrep, trufflehog, pip-audit]
  patterns: [reusable-workflows, workflow-call, concurrency-groups]

key_files:
  created:
    - .github/workflows/ci.yml
    - .github/workflows/code-quality.yml
    - .github/workflows/tests.yml
    - .github/workflows/security.yml
    - .codecov.yml
  modified: []

decisions:
  - Used pre-commit/action@v3.0.1 for code quality instead of manual pre-commit run
  - Used uvx for pip-audit to avoid adding it as a dev dependency
  - Used semgrep/semgrep-action@v1 for Semgrep instead of uvx
  - Used trufflesecurity/trufflehog@main action for TruffleHog
  - Concurrency cancel-in-progress only on PRs, not push to main

requirements-completed: [INF-03]

metrics:
  duration: 3min
  completed: 2026-03-28T01:41:31Z
  tasks_completed: 1
  tasks_total: 1
  files_created: 5
  files_modified: 0
---

# Phase 01 Plan 03: GitHub Actions CI Pipeline Summary

Reusable GitHub Actions CI pipeline with code quality, test, and security workflows following the discogsography pattern, plus Codecov configuration with precision 2, 70-100% range, project auto/1% threshold, and patch 80%/5% threshold.

## Task Summary

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | Create reusable CI workflows and Codecov config | f0c2686 | .github/workflows/ci.yml, code-quality.yml, tests.yml, security.yml, .codecov.yml |

## What Was Built

### Main CI Entrypoint (.github/workflows/ci.yml)
- Triggers on push (all branches) and pull_request (all branches)
- Concurrency group `ci-${{ github.ref }}` with `cancel-in-progress` only on PRs
- Calls three reusable workflows: code-quality, tests, security
- Tests depend on quality passing first; security runs independently

### Code Quality Workflow (.github/workflows/code-quality.yml)
- Reusable via `workflow_call`
- Uses `pre-commit/action@v3.0.1` to run all pre-commit hooks
- Sets up Python 3.13 and uv

### Tests Workflow (.github/workflows/tests.yml)
- Reusable via `workflow_call`
- PostgreSQL 16-alpine service container with health checks
- Runs `pytest --cov=phaze --cov-report=xml --cov-report=term-missing`
- Uploads coverage to Codecov with `flags: unittests`, `disable_search: true`
- DATABASE_URL environment variable for test database connection

### Security Workflow (.github/workflows/security.yml)
- Reusable via `workflow_call`
- Runs pip-audit via `uvx pip-audit`
- Runs bandit: `bandit -r src/ -x tests -s B608`
- Runs Semgrep via `semgrep/semgrep-action@v1` with auto config
- Runs TruffleHog via `trufflesecurity/trufflehog@main` with `--only-verified`

### Codecov Configuration (.codecov.yml)
- Precision: 2, round: down, range: 70-100%
- Project status: auto target, 1% threshold
- Patch status: 80% target, 5% threshold

## Deviations from Plan

None -- plan executed exactly as written.

## Known Stubs

None -- all files are complete CI configuration with no placeholders.

## Self-Check: PASSED

- All 5 created files verified on disk
- All 1 commit hash verified in git log
- SUMMARY.md created at expected path
