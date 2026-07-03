---
phase: 64-per-module-coverage-uplift-gate-raise
reviewed: 2026-07-02T00:00:00Z
depth: standard
files_reviewed: 8
files_reviewed_list:
  - scripts/coverage_floor.py
  - tests/shared/test_coverage_floor.py
  - tests/shared/test_coverage_gate.py
  - tests/shared/test_ci_workflow_wiring.py
  - tests/review/services/test_review_degrade.py
  - tests/agents/services/test_agent_liveness.py
  - justfile
  - pyproject.toml
findings:
  critical: 0
  warning: 3
  info: 2
  total: 5
status: issues_found
---

# Phase 64: Code Review Report

**Reviewed:** 2026-07-02T00:00:00Z
**Depth:** standard
**Files Reviewed:** 8
**Status:** issues_found

## Summary

Phase 64 adds a per-module coverage floor gate (`scripts/coverage_floor.py`, uniform 85%) that
runs on the COMBINED `coverage.json` inside `just coverage-combine`, and raises the global
combined gate from the 90.38% baseline to 95% at two edit sites (`pyproject.toml fail_under` and
the justfile `coverage report --fail-under=95`). Three guard test files and two coverage-uplift
test files round out the phase.

The gate-sync mechanics are sound: `pyproject.toml fail_under = 95` and the justfile
`--fail-under=95` agree, and `test_coverage_gate.py` structurally enforces they stay equal. The
floor script fails closed on the two tested failure modes (missing file → `FileNotFoundError`,
empty string → `JSONDecodeError`). The uplift tests (`test_review_degrade.py`,
`test_agent_liveness.py`) assert observable outcomes, not tautologies.

Three defects reduce robustness / trustworthiness of the load-bearing gate: (1) the floor script
fails OPEN on a parseable-but-empty `{"files": {}}` report — the one "empty input" case its own
docstring promises will raise; (2) the "exact-value invariant" that `test_ci_workflow_wiring.py`
explicitly delegates to `test_coverage_gate.py` is never actually enforced there; and (3) the
gate raise to 95 left a trail of stale "85" descriptions inside the load-bearing guard files.

No Critical issues. No security issues (both floor/gate scripts read local trusted files; no
injection, eval, or secret surface introduced).

## Warnings

### WR-01: Floor script fails OPEN on a parseable-but-empty `{"files": {}}` report

**File:** `scripts/coverage_floor.py:38-55`
**Issue:** The module docstring (lines 17-22) promises the gate FAILS CLOSED: *"a missing / empty /
unparseable `coverage.json` raises ... A missing gate input NEVER exits 0."* But an **empty files
dict** — valid JSON, e.g. `{"files": {}}` — is neither missing nor unparseable, so it flows
through: the `for` loop iterates zero times, `failures` stays empty, and `main()` returns `0`,
printing `✅ All tracked modules ≥ 85%`. This is the exact "empty input" class the docstring says
raises. If `coverage combine` / `coverage json` ever emits an empty (or mis-sourced) file set —
stale `.coverage.*` shards, a wrong CWD, a `source=` misconfiguration — the per-module gate goes
green on ZERO tracked modules. `test_coverage_floor.py` tests the empty-string and missing-file
cases but NOT the empty-dict case, so the hole is uncovered. (In-recipe the prior `coverage report`
step would usually catch a truly dataless run, but the script's standalone contract — the thing the
test file exists to protect — is violated, and the floor is designed to run as an independent gate.)
**Fix:** Fail closed when the tracked set is empty, and add a guard test:
```python
def main() -> int:
    data = json.loads(Path("coverage.json").read_text(encoding="utf-8"))
    files = data["files"]
    if not files:  # empty/mis-sourced report is a gate failure, not an all-clear
        print("❌ coverage.json has no tracked files — refusing to pass (fail closed).")  # noqa: T201
        return 1
    ...
```
```python
def test_empty_files_dict_fails_closed(tmp_path, monkeypatch):
    module = _load_floor_module()
    _write_coverage_json(tmp_path, {})  # {"files": {}}
    monkeypatch.chdir(tmp_path)
    assert module.main() != 0
```

### WR-02: The "exact-value invariant" is delegated to a guard that never enforces it

**File:** `tests/shared/test_ci_workflow_wiring.py:96` and `tests/shared/test_coverage_gate.py:66-82`
**Issue:** `test_ci_workflow_wiring.py` line 96 explicitly justifies its own weak assertion by
delegating ownership: *"test_coverage_gate.py owns the exact-value invariant, so here we only assert
the recipe still enforces a global fail-under gate at all."* But `test_coverage_gate.py`'s
`test_global_gate_sites_agree_and_beat_the_baseline` only asserts the two sites are **equal** and
**both `> 90.38`** — it never asserts the exact value (95). The claimed "exact-value invariant" does
not exist in either file. Consequence: a silent regression that lowers BOTH sites in lockstep to any
value above the baseline (e.g. `92`) passes every guard green, even though the phase's stated target
(and the justfile/pyproject) is 95. The task's own framing ("must stay in sync **at 95**") is
therefore unguarded.
**Fix:** Add an explicit expected-value assertion in `test_coverage_gate.py` so the delegation is
truthful:
```python
_EXPECTED_GATE = 95  # Phase 64 D-05 target; bump deliberately with the two edit sites.
...
    assert pyproject_gate == _EXPECTED_GATE, f"gate regressed from {_EXPECTED_GATE} to {pyproject_gate}"
```

### WR-03: Stale "85" descriptions of the global gate inside the load-bearing guard files

**File:** `tests/shared/test_ci_workflow_wiring.py:9,18,80,91`; `justfile:96,98`
**Issue:** The gate raise to 95 left multiple comments/docstrings still describing the GLOBAL
combined gate as "85%":
- `test_ci_workflow_wiring.py:9` — "enforcing the pyproject-wide 85% gate against one bucket"
- `test_ci_workflow_wiring.py:18` — "the 85% gate is enforced once"
- `test_ci_workflow_wiring.py:80` — "pytest-cov enforces pyproject's fail_under=85"
- `test_ci_workflow_wiring.py:91` — docstring: "enforces the 85% gate on the COMBINED number"
- `justfile:96,98` — "pyproject's fail_under=85" / "The 85% gate is enforced once" (the justfile one
  was pre-flagged as known non-blocking in the review brief; listed here for completeness)

These are the highest-traffic explanations of exactly how the gate works, sitting inside the guard
that protects it. A future maintainer reading `test_ci_workflow_wiring.py` to understand the gate
will be told the wrong number. Note the per-module FLOOR genuinely IS 85 (so `coverage_floor.py`'s
"85" is correct) — only the references to the *global pytest-cov / combine* gate are stale.
**Fix:** Update each of the above to "95%" (per-module floor stays 85%). Prefer wording that names
both tiers to prevent the next drift, e.g. "the combined 95% gate (per-module floor is 85%)".

## Info

### IN-01: Floor-script docstring "Exit semantics" omits the empty-dict path

**File:** `scripts/coverage_floor.py:17-22`
**Issue:** Related to WR-01: the "Exit semantics" block enumerates 0 / 1 / non-zero but never
addresses the `{"files": {}}` case, which currently returns 0. Once WR-01 is fixed, document that an
empty tracked set exits 1.
**Fix:** After fixing WR-01, add a line: "1 — the tracked-file set is empty (a mis-sourced or
dataless combined report is treated as a gate failure, never an all-clear)."

### IN-02: `coverage_floor.py` header references D-04=85 uniform floor; keep floor constant self-documenting

**File:** `scripts/coverage_floor.py:31`
**Issue:** `FLOOR = 85.0` is a magic value whose meaning ("per-module floor, distinct from the 95%
combined gate") is only recoverable from the prose docstring. Given two different thresholds now
coexist (85 per-module, 95 combined), an inline comment on the constant reduces the chance a future
edit conflates them.
**Fix:** `FLOOR = 85.0  # per-MODULE floor; distinct from the 95% COMBINED gate (pyproject/justfile).`

---

_Reviewed: 2026-07-02T00:00:00Z_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
