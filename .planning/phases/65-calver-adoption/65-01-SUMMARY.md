---
phase: 65-calver-adoption
plan: 01
subsystem: ci-versioning-tests
tags: [calver, tests, red-gate, structural-guard, ci]
requires:
  - ".github/workflows/ci.yml (current v*.*.* glob — the RED target)"
  - ".planning/MILESTONES.md (exists; no mapping table yet — RED target)"
  - "docs/deployment.md (exists; no CalVer prose yet — RED target)"
provides:
  - "test_ci_workflow_triggers_on_version_tags retargeted to the CalVer glob (present) + v*.*.* (absent)"
  - "test_milestones_mapping_table_intact (VER-04) structural guard"
  - "test_calver_scheme_documented (VER-01) structural guard"
  - "de-v'ed docker-publish + module docstrings (D-06)"
affects:
  - "Plan 02 (edits ci.yml / MILESTONES.md / docs to turn these 3 gates GREEN)"
tech-stack:
  added: []
  patterns:
    - "structural pytest guard: parents[3] path constant + exists-guard + read + substring assert + fix-instruction message"
    - "CalVer glob literal [0-9]+.[0-9]+.[0-9]+ shared between ci.yml and its guard test"
key-files:
  created: []
  modified:
    - "tests/agents/deployment/test_agent_compose.py"
decisions:
  - "The D-16 assertion *message* on line 191 (:v<version> -> :<version>) was also de-v'ed to satisfy the grep-count-0 acceptance criterion; message-only, assertion logic unchanged (D-06 not weakened)."
  - "test_calver_scheme_documented asserts combined (deployment.md + MILESTONES.md) membership so Plan 02 can place the prose in either file; month-rule = 'leading-zero'/'leading zero' + 'month', revision-rule = 'revision' + ('zero-based'|'per-month'|'resets')."
metrics:
  duration: ~8 min
  completed: 2026-07-03
---

# Phase 65 Plan 01: CalVer RED Test Gate Summary

Authored the executable RED definition of "CalVer is adopted" in the single existing module `tests/agents/deployment/test_agent_compose.py`: retargeted the one real CI-glob guard to the bare CalVer pattern, added two new structural guards (milestone↔version mapping table + CalVer-scheme-documented), and de-`v`-ed the docker-publish/module docstrings — all landing RED against the current `v*.*.*` repo, to be turned GREEN by Plan 02.

## What Was Built

**Task 1 — Retarget CI glob guard + de-v docstrings (commit 9eab593):**
- `test_ci_workflow_triggers_on_version_tags`: introduced local `CALVER_GLOB = "[0-9]+.[0-9]+.[0-9]+"`, retargeted the positive assertion to `any(CALVER_GLOB in str(t) for t in tags)`, and ADDED the CalVer-only negative assertion `assert not any("v*.*.*" in str(t) for t in tags)` (D-02). Reused `_load_ci_workflow_triggers` verbatim; kept the branch-preservation block byte-for-byte. Docstring rewritten to bare CalVer `YYYY.MM.REVISION` / first tag `2026.7.0` wording.
- `test_docker_publish_workflow_tags_both_latest_and_version`: docstring-only edit `:v<version>` -> `:<version>` (D-06); assertion body (`type=semver` / `type=ref,event=tag`) untouched. The D-16 assertion message string was also de-v'ed (message-only).
- Module-level docstring `:v<version>` -> `:<version>`.
- `test_ci_detect_changes_forces_code_changed_on_tags` left unchanged (proves D-03).

**Task 2 — Two new structural guards (commit 7309429):**
- Added module-level `MILESTONES_PATH` (repo-root `.planning/MILESTONES.md`) and `DEPLOYMENT_DOC_PATH` (`docs/deployment.md`) constants in the `parents[3]` idiom.
- `test_milestones_mapping_table_intact` (VER-04): asserts a `| Milestone | Version | Date |` header line (column-order-robust), each `v1.0`..`v7.0` verbatim (D-10), and the `2026.7.0` row (D-01/D-11), with a per-token divergence message.
- `test_calver_scheme_documented` (VER-01): combined-text membership over both docs asserting `YYYY.MM.REVISION`, `2026.7.0`, the no-leading-zero month rule, and the per-month zero-based REVISION convention (D-07).
- No new imports (Path/re/yaml already present).

## Verification

- `uv run pytest tests/agents/deployment/test_agent_compose.py -q`: **3 failed, 10 passed** — exactly the 3 CalVer-gating tests are RED (live gates), all other structural guards green.
- RED proof: `test_ci_workflow_triggers_on_version_tags`, `test_milestones_mapping_table_intact`, `test_calver_scheme_documented` all fail against the current repo with fix-instruction messages naming the CalVer glob / mapping table / scheme prose.
- GREEN (unchanged) proof: `test_docker_publish_workflow_tags_both_latest_and_version` + `test_ci_detect_changes_forces_code_changed_on_tags` pass.
- Collection clean (`--collect-only` lists both new tests, no ImportError/SyntaxError).
- `uv run ruff check` + `ruff format --check` clean; full pre-commit suite (ruff, ruff-format, bandit, mypy) passed on both commits.

## Deviations from Plan

None — plan executed as written. One acceptance-criterion-driven extra: the D-16 assertion *message* on line 191 was de-v'ed alongside the docstrings so `grep -c ':v<version>'` returns 0 (the plan's acceptance criterion); this is a message string, not assertion logic, so D-06 "retarget, don't weaken" is preserved.

## Authentication Gates

None.

## Notes for Plan 02

The three RED gates define the exact contract Plan 02 must satisfy:
- `ci.yml` line 13: `tags: ["v*.*.*"]` -> `tags: ["[0-9]+.[0-9]+.[0-9]+"]` (quoted).
- `.planning/MILESTONES.md`: add a `| Milestone | Version | Date |` table with `v1.0`..`v7.0` verbatim rows + a `2026.7.0` row.
- `docs/deployment.md` and/or MILESTONES.md: document `YYYY.MM.REVISION`, `2026.7.0`, the no-leading-zero month rule, and the per-month zero-based REVISION convention. The month-rule check matches `("leading-zero" | "leading zero") AND "month"`; the revision-rule check matches `"revision" AND ("zero-based" | "per-month" | "resets")` (case-insensitive).

## Self-Check: PASSED

- `tests/agents/deployment/test_agent_compose.py` modified — verified present.
- Commits verified in git log: 9eab593 (Task 1), 7309429 (Task 2), 1802b1f (SUMMARY).
- Working tree clean.
