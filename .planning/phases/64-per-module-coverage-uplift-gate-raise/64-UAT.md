---
status: complete
phase: 64-per-module-coverage-uplift-gate-raise
source: [64-01-SUMMARY.md, 64-02-SUMMARY.md, 64-03-SUMMARY.md, 64-04-SUMMARY.md]
started: 2026-07-02
updated: 2026-07-02
---

## Current Test

[testing complete]

## Tests

### 1. Per-module floor catches a sub-floor module
expected: Running `scripts/coverage_floor.py` on a combined coverage.json where a module is below 85% exits 1 and prints the offending path + percentage.
result: pass
evidence: review.py at 83.16% → "❌ Per-module coverage floor 85% not met: 83.16% src/phaze/services/review.py", exit 1.

### 2. Floor script fails CLOSED on broken input
expected: An empty `{"files": {}}` report and a missing coverage.json both exit NON-ZERO — never a false all-clear (T-64-01, WR-01).
result: pass
evidence: empty-files-dict → "refusing to pass an empty coverage report", exit 1; missing file → exit 1.

### 3. Floor passes when all modules meet the floor
expected: All tracked modules ≥ 85% exits 0; zero-statement files (`__init__.py`) are skipped, not failed.
result: pass
evidence: main.py 95% + __init__.py 0-stmt → "✅ All tracked modules ≥ 85%", exit 0.

### 4. Global gate raised to 95, in sync at both sites
expected: `pyproject.toml fail_under` and justfile `coverage report --fail-under` both read 95 (> 90.38 baseline), and the guard test enforces equality + baseline + pin.
result: pass
evidence: pyproject `fail_under = 95`, justfile `--fail-under=95`; `test_coverage_gate.py` 2 passed.

### 5. `just coverage-combine` runs both guardrails
expected: The recipe emits `coverage json` then runs `coverage report --fail-under=95` AND `scripts/coverage_floor.py` on the combined coverage.
result: pass
evidence: recipe order confirmed: combine → xml → json → report --fail-under=95 → python scripts/coverage_floor.py.

### 6. A red coverage gate blocks merge (CI enforcement, fail-closed)
expected: main is gated by a required status check that a red `just coverage-combine` would fail — enforcement, not advisory (COV-02).
result: pass
evidence: live ruleset `aggregate-results` (active, target ~DEFAULT_BRANCH) requires the `aggregate-results` check; chain ci.yml aggregate-results (deny-list, needs test) ← test uses tests.yml ← combine runs `just coverage-combine`. Red gate → red combine → red test → red aggregate-results → merge blocked.

## Summary

total: 6
passed: 6
issues: 0
pending: 0
skipped: 0
blocked: 0

## Gaps

[none]
