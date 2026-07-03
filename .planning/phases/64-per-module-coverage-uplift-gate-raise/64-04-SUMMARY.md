---
plan: 64-04
phase: 64-per-module-coverage-uplift-gate-raise
status: complete
requirements: [COV-02]
checkpoint_outcome: approved
verdict: PASS
---

# 64-04 Summary — Confirm the coverage gate is ENFORCING (COV-02)

## Outcome: PASS — COV-02 fully delivered (machinery + CI enforcement)

The raised global gate + per-module floor are **fail-closed at the branch-protection
layer**. A red `just coverage-combine` (from either the 95% global gate or the
`scripts/coverage_floor.py` per-module 85% floor) surfaces as a red **required** status
check on `main` and blocks merge. No operator action was needed — enforcement was already
in place via a repository ruleset.

## Task 1 — required status checks on main (corrected)

**Initial read used the WRONG API.** `gh api repos/SimplicityGuy/phaze/branches/main/protection/required_status_checks`
returned `HTTP 404 "Branch not protected"`. That is a **false gap**: the legacy
branch-protection REST API is blind to repository **rulesets**, which is how this repo
enforces main. (Recorded as a standing correction in project memory:
`feedback_use_rulesets_not_legacy_branch_protection` — always query the rulesets API here.)

**Authoritative read via the rulesets API:**
- `gh api repos/SimplicityGuy/phaze/rulesets` → one ruleset: `aggregate-results` (id 18454947), enforcement **active**, target `branch`.
- `gh api repos/SimplicityGuy/phaze/rulesets/18454947` →
  - conditions: `ref_name.include = ['~DEFAULT_BRANCH']` (main)
  - rules: `deletion`, `non_fast_forward`, and **`required_status_checks` = `['aggregate-results']`**

**Determination: PASS.** The `aggregate-results` context IS a required, merge-blocking
status check on main.

## Task 2 — human-verify checkpoint (blocking): confirmed enforcing

The operator confirmed (and pointed out) the active `aggregate-results` ruleset. Verified the
full propagation chain — a coverage breach cannot merge:

1. `pyproject.toml [tool.coverage.report] fail_under = 95` **and** `justfile coverage-combine`
   → `coverage report --fail-under=95` then `python scripts/coverage_floor.py` (per-module 85% floor).
2. `.github/workflows/tests.yml` job **`combine`** runs `just coverage-combine`; a non-zero exit fails the job.
3. `.github/workflows/ci.yml` job **`test`** → `uses: ./.github/workflows/tests.yml`; a failed `combine` fails `test`.
4. `.github/workflows/ci.yml` job **`aggregate-results`** (`needs: [detect-changes, quality, test, security, docker]`, `if: always()`)
   applies a **deny-list** gate: a job passes only when it explicitly `succeeded`. `TEST_RESULT != success` → `exit 1`.
5. Ruleset requires the `aggregate-results` check → red `aggregate-results` **blocks merge to main**.

Net: red gate/floor → red `combine` → red `test` → red `aggregate-results` → merge blocked. Fail-closed (closes RESEARCH A3 / Open-Q1; T-64-07 mitigated).

## Notes / cross-references

- The Phase 63 UAT #11 deferred chore ("set GitHub branch-protection required-check =
  `aggregate-results`") is **satisfied** — done via the ruleset, not legacy branch protection.
  No new Deferred Item is carried forward; ROADMAP Success Criterion #3 (unconditional
  merge-blocking gate) is **MET**.
- Files changed: none (GitHub settings inspection + this SUMMARY only), as the plan specified.
