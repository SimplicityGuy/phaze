---
phase: 63-parallel-ci-code-change-gating
reviewed: 2026-07-02T00:00:00Z
depth: standard
files_reviewed: 8
files_reviewed_list:
  - scripts/classify-changed-files.sh
  - .github/workflows/tests.yml
  - .github/workflows/ci.yml
  - justfile
  - pyproject.toml
  - tests/buckets.json
  - tests/shared/test_partition_guard.py
  - tests/shared/test_change_gate.py
findings:
  critical: 1
  warning: 2
  info: 2
  total: 5
status: issues_found
---

# Phase 63: Code Review Report

**Reviewed:** 2026-07-02
**Depth:** standard
**Files Reviewed:** 8
**Status:** issues_found

## Summary

Reviewed the Parallel-CI + code-change-gating implementation: the doc/code classifier, the
setup→matrix→combine test topology, the CI aggregate/skip-with-success contract, the justfile
recipes that glue them, and the two guard tests.

Most of the design is sound and errs conservative in the intended places:

- **CODECOV_TOKEN scoping is correct** — the token is referenced only in the `combine` job's
  Codecov step (`tests.yml:147-154`); the fan-out matrix jobs never see it.
- **Combine topology is correct** — `download-artifact` with `pattern: coverage-*` +
  `merge-multiple: true` pulls every shard, and `just coverage-combine` runs
  `coverage combine → coverage xml → coverage report --fail-under=85`, so the **85% gate is
  preserved** and enforced before the Codecov upload.
- **Failing-bucket masking is prevented on the matrix side** — `fail-fast: false` runs all
  buckets, `combine` has no `if: always()` so it only runs when every shard succeeded, and
  `upload-artifact` uses `if-no-files-found: error`.
- **The two guard tests are real behavioral assertions**, not smoke — see the closing note.

However, there is a **fail-open hole in the CI aggregate gate** (CR-01) that lets the entire
test/security/docker suite be skipped while the *required* check still reports green, plus two
narrower fail-open gaps in the classifier. Because the whole point of this phase is the gating
contract, CR-01 must be fixed before this ships.

## Critical Issues

### CR-01: `aggregate-results` reports SUCCESS when `detect-changes` fails or when required legs are skipped/cancelled (fail-open gate)

**File:** `.github/workflows/ci.yml:120-167`

**Issue:** The aggregate gate is the single required status check, but it only ever fails on the
literal string `"failure"` and it never inspects `needs.detect-changes.result`. Two concrete
fail-open paths:

1. **`detect-changes` fails.** The `run:` block executes `git diff ...` under bash `set -e`
   (GitHub's default `bash -eo pipefail`). Any git error (bad/absent SHA, transient fetch issue)
   fails the step → the job fails → the `code-changed` output is **never set** →
   `needs.detect-changes.outputs.code-changed` evaluates to `""`.
   - `test` (`needs: [detect-changes, quality]`), `security`, and `docker` all list
     `detect-changes` in `needs`, so a failed `detect-changes` makes each of them **skipped**
     (result `"skipped"`), regardless of their `if:`.
   - In `aggregate-results`, `CODE_CHANGED=""` → the `== "false"` docs-only branch is NOT taken,
     but the code-path check only tests `TEST_RESULT/SECURITY_RESULT/DOCKER_RESULT == "failure"`.
     `"skipped" != "failure"`, so the step prints "All pipeline workflows passed!" and **exits 0**.
   - Net effect: a transient failure in the change-detector produces a **green required check with
     zero tests, security scans, or docker validation ever run.** A PR with genuinely failing tests
     can merge.

2. **A required leg is `cancelled`.** `cancel-in-progress` is enabled for PRs. A leg that ends
   `"cancelled"` (or any state other than `failure`/`success`) is likewise treated as passing,
   because the gate only compares against `"failure"`.

The gate is written as an allow-list of the one bad value (`failure`) instead of a deny-list of the
one good value (`success`). Anything unexpected is silently accepted.

**Fix:** Gate on `detect-changes` succeeding, and on the code path require each leg to be exactly
`success` (docs-only still permits `skipped`):

```bash
# detect-changes MUST have run cleanly — otherwise scope is unknown, fail closed.
if [[ "${DETECT_RESULT}" != "success" ]]; then
  echo "❌ Change detection did not complete (result: ${DETECT_RESULT}) — failing closed"
  exit 1
fi

if [[ "${QUALITY_RESULT}" != "success" ]]; then
  echo "❌ Code quality did not pass (result: ${QUALITY_RESULT})"
  exit 1
fi

if [[ "${CODE_CHANGED}" == "false" ]]; then
  echo "📄 Docs-only change — skipped heavy jobs are expected"
  exit 0
fi

# Code path: every required leg must be success, not merely "not failure".
for r in "${TEST_RESULT}" "${SECURITY_RESULT}" "${DOCKER_RESULT}"; do
  if [[ "${r}" != "success" ]]; then
    echo "❌ A required workflow did not pass (test=${TEST_RESULT} security=${SECURITY_RESULT} docker=${DOCKER_RESULT})"
    exit 1
  fi
done
echo "✅ All pipeline workflows passed!"
```

Add `DETECT_RESULT: ${{ needs.detect-changes.result }}` to the step `env:` block.

## Warnings

### WR-01: Empty classifier input yields `code-changed=false` (skip) — contradicts the stated fail-safe

**File:** `scripts/classify-changed-files.sh:33-39` (enshrined by `tests/shared/test_change_gate.py:56`)

**Issue:** The script's own header (lines 12-16) and the phase's fail-safe property require that an
**unknown/empty** change set run the full pipeline. The implementation does the opposite: empty
stdin (or all-blank input) makes `code_files` empty → prints `code-changed=false` → CI skips
tests/security/docker. The parametrized test `("", "code-changed=false")` actively locks this
fail-open behavior in as "expected."

Combined with CR-01's `ci.yml` plumbing, if `git diff` ever succeeds but emits an empty list for a
change set that *does* contain code (e.g., an edge-case SHA range, a merge with no net textual diff
against `before`), the classifier returns `false` and the suite is skipped with a green gate.
"No detectable diff" is exactly the *unknown* state that should fail toward running CI, not skip it.

**Fix:** Treat empty/blank input as code-changed=true (fail-safe), and update the corresponding test:

```bash
# Fail safe: an empty or all-blank change set is "unknown", so run the full pipeline.
if [[ -z "${code_files}" ]]; then
  # Distinguish "genuinely empty stdin" from "all paths were docs".
  # Only an all-docs, non-empty input may skip.
  if [[ -z "$(cat_saved_stdin)" ]]; then echo "code-changed=true"; else echo "code-changed=false"; fi
fi
```

Simplest robust form: capture stdin once, and emit `code-changed=false` **only** when the raw input
had at least one non-blank line AND every such line matched a doc pattern; emit `true` otherwise
(including empty input). At minimum, change the empty-stdin case to `code-changed=true` and flip the
`("", ...)` expectation in `test_change_gate.py`.

### WR-02: Over-broad doc patterns — `*.txt` (anywhere) and `docs/**` can hide real code/config changes

**File:** `scripts/classify-changed-files.sh:33`

**Issue:** The doc regex `(\.md$|^\.planning/|^LICENSE$|^docs/|\.txt$)` treats:
- **any `*.txt` anywhere** as documentation, and
- **everything under `docs/`** as documentation.

These are blanket, not fail-safe. A behavior-affecting `*.txt` (e.g. `requirements.txt`,
`runtime.txt`, `constraints.txt`, or a data/allow-list file the app reads at runtime) or a code file
placed under `docs/` (e.g. `docs/conf.py`, a build hook) would classify the whole change set as
docs-only and **skip the entire suite**. This is currently latent for this repo (no tracked `.txt`
files; `docs/` holds only `.md` + static `.html` assets, confirmed via `git ls-files`), but the
classifier is meant to be conservative "by construction," and this violates that for two common
path shapes.

**Fix:** Narrow the doc allow-list to genuinely inert paths. Prefer explicit doc locations over
broad extensions, e.g. drop the blanket `\.txt$`, and if `docs/` must be treated as docs, restrict
it to known-inert suffixes:

```bash
# Only treat text/markdown assets under docs/ as docs; never a bare *.txt anywhere.
grep -vE '(\.md$|^\.planning/|^LICENSE$|^docs/.*\.(md|txt|html|rst)$)'
```

Anything not provably documentation should keep `code-changed=true`.

## Info

### IN-01: PR diff uses two-dot (`BASE HEAD`) rather than three-dot (`BASE...HEAD`)

**File:** `.github/workflows/ci.yml:64`

**Issue:** `git diff --name-only "${BASE_SHA}" "${HEAD_SHA}"` is a two-dot diff, which includes
files that changed on the base branch since the PR forked (not just files the PR introduced). This
errs **safe** (extra files → more likely `code-changed=true`), so it is not a correctness defect —
but on a base branch that moved, a genuinely docs-only PR may unnecessarily run the full pipeline.

**Fix:** Use three-dot `git diff --name-only "${BASE_SHA}...${HEAD_SHA}"` to scope to the PR's own
changes (matches the merge-base semantics GitHub uses for the PR "Files changed" view).

### IN-02: Test and combine jobs run `just install`, which downloads the Tailwind binary over the network

**File:** `.github/workflows/tests.yml:94-95, 135-136` (via `justfile:18, 63-73`)

**Issue:** `install:` depends on `tailwind`, which `curl`s the standalone Tailwind binary from
GitHub Releases. Every one of the 9 matrix test shards and the combine job therefore performs a
network download that is irrelevant to running pytest or combining coverage. This is a CI
reliability/flakiness surface (a GitHub Releases hiccup fails an otherwise-green test shard), not a
logic bug.

**Fix:** Give the CI jobs a leaner dependency install that skips the CSS build, e.g. a
`just install-ci` recipe running only `uv sync` (no `tailwind` prerequisite), and call it from the
test/combine jobs.

---

## Note on guard-test quality (verified non-vacuous)

Both guard tests are real behavioral assertions, satisfying the "not vacuous/smoke" requirement:

- `tests/shared/test_partition_guard.py` — `test_every_collected_test_lives_in_a_known_bucket`
  actually `rglob`s the tree for **both** default pytest globs (`test_*.py` and `*_test.py`,
  closing the historical `*_test.py` blind spot) and asserts each collected file's top segment is a
  known bucket. `test_meta_guard_flags_unbucketed` proves the membership logic is not vacuously
  green by pushing a synthetic out-of-bucket path and a bare-root path through the same function and
  asserting they are flagged. All 9 buckets in `buckets.json` were confirmed to exist as
  directories.
- `tests/shared/test_change_gate.py` — runs the **real** classifier over a subprocess (the exact
  CI interface) and `test_mixed_doc_and_code_is_conservative` makes an explicit positive assertion
  that docs+`.py` → `code-changed=true` (the T-63-04-01 security invariant). Caveat: the parametrized
  `("", "code-changed=false")` row locks in the WR-01 fail-open and should be revised alongside that
  fix.

---

_Reviewed: 2026-07-02_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
