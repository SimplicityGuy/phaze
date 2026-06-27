---
phase: quick-260606-pjd
plan: 01
subsystem: ci
tags: [ci, github-actions, force-push, detect-changes]
requires: []
provides:
  - "ci.yml detect-changes force-push reachability fallback"
  - "guard test locking the fallback in place"
affects:
  - .github/workflows/ci.yml
  - tests/test_deployment/test_agent_compose.py
tech-stack:
  added: []
  patterns:
    - "git cat-file -e reachability probe as a shell condition (set -e safe)"
key-files:
  created: []
  modified:
    - .github/workflows/ci.yml
    - tests/test_deployment/test_agent_compose.py
decisions:
  - "Reuse the existing zero-SHA origin/main...HEAD fallback for the force-push case rather than adding a new branch"
metrics:
  duration: ~6m
  completed: 2026-06-06
---

# Phase quick-260606-pjd Plan 01: Make ci.yml detect-changes robust to force-pushes Summary

Force-pushed branches no longer fail CI with `fatal: bad object`; the push-event
detect-changes filter step now probes whether `github.event.before` is reachable and
falls back to `git diff origin/main...HEAD` when it is not.

## What Changed

### Task 1: Force-push reachability fallback in ci.yml
Extended the `detect-changes` job's `id: filter` run script. The existing zero-SHA
`elif` now also fires when `BEFORE_SHA` is unreachable in the fresh CI clone:

```yaml
elif [[ "${BEFORE_SHA}" == "0000000000000000000000000000000000000000" ]] || ! git cat-file -e "${BEFORE_SHA}^{commit}" 2>/dev/null; then
  # New branch (zero SHA) or force-pushed branch whose before-SHA is gone — compare against default branch
  CHANGED_FILES=$(git diff --name-only "origin/main...${HEAD_SHA}")
```

- `! git cat-file -e ...` is used as a condition, so it does not abort under `set -e`.
- `BEFORE_SHA` stays quoted (shellcheck/actionlint clean).
- The schedule/dispatch/tag early-exit, the `pull_request` path, and the normal-push
  `else` body are byte-for-byte unchanged.

### Task 2: Guard test
Added `test_ci_detect_changes_survives_force_push` in
`tests/test_deployment/test_agent_compose.py`, directly after the tag-forcing test and
reusing `_ci_detect_changes_filter_step()`. It asserts the filter step's run script
contains BOTH `git cat-file -e` (reachability probe) and `origin/main...` (default-branch
fallback). The test fails loudly if the reachability fallback is removed.

## Verification

- `uv run pytest tests/test_deployment/test_agent_compose.py -q` → 8 passed.
- `pre-commit run --all-files` → all hooks pass (actionlint, yamllint strict,
  check-jsonschema, ruff, ruff-format, bandit, mypy, shellcheck). No `--no-verify`.

## Deviations from Plan

None - plan executed exactly as written.

## Commits

- `322a3aa` fix(260606-pjd): handle force-push before-SHA in ci detect-changes
- `d89a00b` test(260606-pjd): guard force-push fallback in ci detect-changes

## Self-Check: PASSED

- FOUND: .github/workflows/ci.yml (contains `git cat-file -e` and `origin/main...`)
- FOUND: tests/test_deployment/test_agent_compose.py (contains `test_ci_detect_changes_survives_force_push`)
- FOUND commit: 322a3aa
- FOUND commit: d89a00b
