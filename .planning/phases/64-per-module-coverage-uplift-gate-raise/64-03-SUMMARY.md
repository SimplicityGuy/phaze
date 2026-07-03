---
phase: 64-per-module-coverage-uplift-gate-raise
plan: 03
subsystem: testing
tags: [coverage, ci-gate, fail-under, coverage-floor, guard-test]
requires:
  - "scripts/coverage_floor.py (64-01 — per-module 85% floor script)"
  - "services/review.py + agent_liveness.py uplift (64-02 — cleared the only sub-floor module)"
provides:
  - "global coverage gate raised 85 -> 95 at both edit sites (D-05)"
  - "per-module floor wired into `just coverage-combine` on the combined coverage (D-01/D-02)"
  - "gate-consistency guard test (the two fail_under sites cannot silently drift)"
affects:
  - "CI `combine` job — now fails on overall < 95 OR any tracked module < 85 (fail-closed)"
tech-stack:
  added: []
  patterns:
    - "single combine seam runs BOTH guardrails (global --fail-under + per-module floor script)"
    - "tomllib + _extract_recipe regex guard asserting two config sites agree (mirrors test_ci_workflow_wiring)"
key-files:
  created:
    - "tests/shared/test_coverage_gate.py"
  modified:
    - "justfile"
    - "pyproject.toml"
    - "tests/shared/test_ci_workflow_wiring.py"
    - ".gitignore"
decisions:
  - "NEW_GLOBAL pinned at 95 (integer): measured combined overall 97.12% post-uplift, minus ~2pt headroom (D-05, T-64-06 accept — deliberate margin, not over-tight)"
  - "Updated test_ci_workflow_wiring's gate assertion to a regex (--fail-under=<digits>) so the exact-value invariant lives solely in the new test_coverage_gate.py"
  - "gitignored coverage.json (new artifact the recipe now emits for the floor script)"
metrics:
  duration: "~25m (incl. ~11m CI-faithful full-suite coverage measurement)"
  completed: 2026-07-03
  tasks: 2
  files: 5
---

# Phase 64 Plan 03: Coverage Gate Raise + Floor Wiring Summary

Raised the enforced global coverage gate from 85% to **95%** at both edit sites and wired the
per-module 85% floor script (from 64-01) into the single `just coverage-combine` seam, so both
guardrails now run on the authoritative COMBINED coverage in CI. Added a stdlib-only guard test
that fails loud if the two gate numbers ever drift or regress to/below the 90.38% baseline.

## Measurement (D-05 — measure-then-set)

Re-measured CI-faithfully at execute time (per-bucket shards → `coverage combine` → `coverage
json`/`report`) against ephemeral Postgres:5433 + Redis:6380, all 9 buckets (discovery, metadata,
fingerprint, analyze, identify, review, agents, integration, shared):

- **Combined overall = 97.12%** (`totals.percent_covered` = 97.11852543…), up from the 96.89%
  pre-uplift baseline — confirms the 64-02 `review.py`/`agent_liveness.py` uplift landed.
- **Lowest tracked module = 86.72%** (`services/agent_client.py`); zero modules below the 85% floor
  — `scripts/coverage_floor.py` exits 0 on the combined data.
- **Chosen NEW_GLOBAL = 95** (integer): `floor(97.12 − ~1)` is 96, but D-05 recommends 95 for a
  measured overall ≥ 96, leaving ~2pt headroom so unrelated future PRs are not brittle-blocked
  (T-64-06 "accept"). Integer avoids `precision`-vs-`fail_under` float edges.

## What Was Built

- **Task 1** — `justfile coverage-combine` now runs, in order: `coverage combine`, `coverage xml`,
  `coverage json` (NEW — the floor script's input), `coverage report --fail-under=95` (raised global
  gate), `python scripts/coverage_floor.py` (NEW — per-module floor). `pyproject.toml
  [tool.coverage.report] fail_under` 85 → 95 (equals the justfile value). `test-bucket`'s
  `--cov-fail-under=0` and `.github/workflows/tests.yml` are untouched — the `combine` job already
  runs `just coverage-combine`, so the floor rides in automatically. `.gitignore` now covers the
  new `coverage.json` artifact.
- **Task 2** — `tests/shared/test_coverage_gate.py` (new, shared bucket, DB-free/subprocess-free):
  parses `pyproject.toml` with `tomllib` and greps the `coverage-combine` recipe's `--fail-under`
  via a copied `_extract_recipe` helper; asserts the two numbers are EQUAL and BOTH strictly > 90.38,
  and that the recipe still emits `coverage json` + invokes `scripts/coverage_floor.py`.

## Verification Results

- Task 1 gate-consistency one-liner → `gate = 95`.
- `uv run pytest tests/shared/test_coverage_gate.py -q` → **2 passed**.
- Negative drift proof: temporarily set pyproject `fail_under = 85` (justfile stays 95) → the guard
  test exits **1** (equality + baseline assertions trip); reverted, tree clean.
- `uv run pytest tests/shared/test_ci_workflow_wiring.py -q` → **6 passed** (updated assertion).
- `uv run pytest tests/shared/test_partition_guard.py -q` → **3 passed** (new file placement valid).
- End-to-end on the measured combined `.coverage`: `coverage report --fail-under=95` exit **0**
  (overall 97.12%); `scripts/coverage_floor.py` exit **0** (all modules ≥ 85%).
- All commits passed the full pre-commit suite (ruff, ruff-format, bandit, mypy) — no `--no-verify`.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Updated pre-existing gate assertion in test_ci_workflow_wiring.py**
- **Found during:** Task 1
- **Issue:** `test_coverage_combine_recipe_enforces_the_gate_exactly_once` hard-asserted
  `"coverage report --fail-under=85" in recipe_body`; raising the recipe to `--fail-under=95` would
  break it — a failure directly caused by this task's change.
- **Fix:** Relaxed that assertion to a regex (`coverage report --fail-under=\d+`) that only checks a
  global fail-under gate exists; the exact-value invariant now lives solely in the new
  `test_coverage_gate.py`. Updated the docstring to note the json step and the ownership split.
- **Files modified:** tests/shared/test_ci_workflow_wiring.py
- **Commit:** 347fba2

**2. [Rule 3 - Blocking] gitignore the new coverage.json artifact**
- **Found during:** Task 1
- **Issue:** `.gitignore` covered `.coverage`, `.coverage.*`, `coverage.xml` but not `coverage.json`,
  which the recipe now emits — it showed as an untracked build artifact.
- **Fix:** Added `coverage.json` next to the `coverage.xml` entry.
- **Files modified:** .gitignore
- **Commit:** 347fba2

## Notes

- No `src/phaze/**` change and no new packages — config edits + a stdlib-only guard test, so nothing
  to vet under the 7-day supply-chain cooldown (T-64-SC).
- `.github/workflows/tests.yml` deliberately unedited (RESEARCH-verified: floor rides inside the
  recipe the `combine` job already calls). No `codecov.yml` exists; Codecov stays advisory.

## Threat Flags

None — no new network endpoints, auth paths, file access, or schema changes.

## Self-Check: PASSED

- FOUND: tests/shared/test_coverage_gate.py
- FOUND: justfile (coverage-combine wired: coverage json + --fail-under=95 + coverage_floor.py)
- FOUND: pyproject.toml (fail_under = 95)
- Commits 347fba2 + 412d501 verified in git log below.
