---
phase: 64-per-module-coverage-uplift-gate-raise
plan: 01
subsystem: ci-coverage
tags: [coverage, ci, gate, stdlib, tdd]
requires: []
provides:
  - "scripts/coverage_floor.py (per-module 85% floor gate over combined coverage.json)"
  - "tests/shared/test_coverage_floor.py (exit-code contract incl. fail-closed)"
affects:
  - "just coverage-combine (a later plan wires the script into the recipe)"
tech-stack:
  added: []
  patterns:
    - "CI-load-bearing script + its own tests/shared/ unit test (Phase 63 classify-changed-files precedent)"
    - "Fail-closed gate: missing/unparseable input raises -> non-zero exit, never 0"
key-files:
  created:
    - scripts/coverage_floor.py
    - tests/shared/test_coverage_floor.py
  modified: []
decisions:
  - "Floor script is stdlib-only (json/sys/pathlib) — zero new deps under the 7-day cooldown (D-02/T-64-SC)"
  - "Tracked set = coverage.json files{} keys, self-maintaining (D-03); EXEMPT dict ships empty (D-09)"
  - "Fail-closed: main() lets a missing/empty coverage.json raise rather than try/except->0 (T-64-01)"
metrics:
  duration: ~6 min
  completed: 2026-07-03
---

# Phase 64 Plan 01: Per-Module Coverage Floor Machinery Summary

Built a stdlib-only `scripts/coverage_floor.py` that fails CI when any tracked `phaze/**`
module falls below the uniform 85% floor over the combined `coverage.json`, plus a
shared-bucket unit test proving its exit-code contract including the fail-closed guard.

## What Was Built

- **`scripts/coverage_floor.py`** — reads `coverage.json` from cwd, iterates `sorted(data["files"].items())`,
  skips `EXEMPT` paths (D-09, dict ships empty) and `num_statements == 0` modules (`__init__.py`),
  compares the raw float `percent_covered` against `FLOOR = 85.0` (D-04), prints a `❌` report and
  returns 1 on any sub-floor module, else prints a `✅` line and returns 0. `main() -> int`, stdlib-only
  (`json`/`sys`/`pathlib`), `from __future__ import annotations`, ruff + mypy clean. Fails closed
  (T-64-01): a missing/empty/unparseable `coverage.json` raises, which propagates as a non-zero exit —
  no try/except returns 0.
- **`tests/shared/test_coverage_floor.py`** — loads the real script via `importlib` and calls `main()`
  over its real interface (`monkeypatch.chdir(tmp_path)` + synthetic `coverage.json`), asserting the
  exit code (D-07 observable outcome) for six cases: (1) sub-floor → exit 1 with the path in stdout,
  (2) all ≥ 85.0 → exit 0 (85.0 boundary passes), (3) zero-statement file at 0% → exit 0, (4) sub-floor
  path added to `EXEMPT` via `monkeypatch.setitem` → exit 0, (5) missing `coverage.json` → raises
  `FileNotFoundError`, (6) empty `coverage.json` → raises `json.JSONDecodeError`. Lives under
  `tests/shared/` so it rides the Phase 63 shared bucket.

## Tasks

| Task | Name | Commit | Files |
| ---- | ---- | ------ | ----- |
| 1 | Create scripts/coverage_floor.py (per-module floor check) | 45d8b2b | scripts/coverage_floor.py |
| 2 | Unit-test the floor script | 104063a | tests/shared/test_coverage_floor.py |

## Verification

- `uv run ruff check scripts/coverage_floor.py` — clean.
- `uv run mypy scripts/coverage_floor.py` — Success, no issues.
- `uv run ruff check tests/shared/test_coverage_floor.py` — clean.
- `uv run pytest tests/shared/test_coverage_floor.py -q` — 6 passed.
- Manual smoke: synthetic sub-floor `coverage.json` → exit 1 (path printed); all-clear (with a
  zero-statement `__init__.py`) → exit 0; missing `coverage.json` → exit 1 (fails closed).
- All commits passed the full pre-commit hook suite (ruff/ruff-format/bandit/mypy) — no `--no-verify`.

## Deviations from Plan

None — plan executed exactly as written. The RESEARCH-authoritative script shape (64-RESEARCH.md
lines 138–181) was implemented verbatim in structure; ruff's import-order autofix (`I001`) and the
test's `TC003` annotation-only import were routine formatting adjustments, not behavior changes.

## Notes

- The plan modifies only `scripts/coverage_floor.py` and `tests/shared/test_coverage_floor.py`. Wiring
  the script into `just coverage-combine` and raising the global `fail_under` (D-05) is a separate plan
  in this phase (RESEARCH Gate Wiring §Q4); this plan builds the machinery only.
- `EXEMPT` intentionally ships empty (D-09): each future entry must carry a written inline justification.

## Self-Check: PASSED

- FOUND: scripts/coverage_floor.py
- FOUND: tests/shared/test_coverage_floor.py
- FOUND commit: 45d8b2b (feat, Task 1)
- FOUND commit: 104063a (test, Task 2)
