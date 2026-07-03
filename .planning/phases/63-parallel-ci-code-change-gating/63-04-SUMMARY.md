---
phase: 63-parallel-ci-code-change-gating
plan: 04
subsystem: ci
tags: [ci, change-gating, github-actions, shellcheck, skip-with-success]
requires:
  - "ci.yml detect-changes + aggregate-results (existing): the ~80%-built skip-with-success gate this plan broadens"
  - "just delegation convention (D-10): CI steps invoke just recipes, not inline shell"
provides:
  - "scripts/classify-changed-files.sh: conservative, unit-testable doc-vs-code classifier (code-changed=true|false on stdin->stdout)"
  - "just detect-code-changes: recipe delegating classification to the script"
  - "ci.yml detect-changes: broadened skip set (*.md, .planning/**, LICENSE, docs/**, *.txt) via the delegated classifier"
  - "tests/shared/test_change_gate.py: regression suite over the classifier incl. the conservative positive case"
affects:
  - ".github/workflows/ci.yml"
  - "justfile"
  - "scripts/classify-changed-files.sh"
  - "tests/shared/test_change_gate.py"
tech-stack:
  added: []
  patterns:
    - "classifier is a versioned shellcheck-clean bash script (set -euo pipefail) invoked via just (D-10), not opaque inline YAML shell"
    - "conservative keep-only-non-doc filter: grep -vE '(\\.md$|^\\.planning/|^LICENSE$|^docs/|\\.txt$)' -> empty remainder == doc-only == code-changed=false"
    - "script stdout is GitHub Actions name=value form (code-changed=...), appended straight to GITHUB_OUTPUT"
    - "regression test drives the real script over subprocess with crafted stdin lists (feed-sample->assert-output meta-test pattern)"
key-files:
  created:
    - "scripts/classify-changed-files.sh"
    - "tests/shared/test_change_gate.py"
  modified:
    - ".github/workflows/ci.yml"
    - "justfile"
decisions:
  - "63-04: broadened the skip set to *.md + .planning/** + LICENSE + docs/** + *.txt; anything not clearly a doc path keeps code-changed=true (conservative security property T-63-04-01)"
  - "63-04: extracted the classifier out of ci.yml into scripts/classify-changed-files.sh so tests/shared/test_change_gate.py can unit-test it (D-09/D-10); ci.yml delegates via just detect-code-changes with a Setup Just step added to detect-changes"
  - "63-04: the SHA edge-case block (schedule/tag, zero-SHA new branch, force-push gone before-SHA) and the aggregate-results skip-with-success contract (if: always() + code-changed==false -> exit 0) left byte-for-byte unchanged; NOT converted to paths-ignore (skip-absent trap)"
metrics:
  duration: ~20min
  completed: 2026-07-02
---

# Phase 63 Plan 04: Broaden Doc-Only CI Skip Summary

Finished CI-04 by broadening the existing doc-only CI skip from `*.md`-only to also cover `.planning/**`, `LICENSE`, `docs/**`, and `*.txt`, extracting the classifier into a versioned, unit-tested `bash` script invoked via a `just` recipe, and adding regression tests — while leaving the `detect-changes` gate topology and the `aggregate-results` skip-with-success contract untouched so a doc-only PR stays mergeable under branch protection.

## What Was Built

**Task 1 — classifier script + just recipe + broadened ci.yml (commit 372609a):**
- Created `scripts/classify-changed-files.sh`: a `set -euo pipefail`, shellcheck-clean (`--severity=warning`) script that reads a newline-delimited changed-file list on stdin and prints exactly `code-changed=false` (every path is documentation) or `code-changed=true`. Classification keeps only non-doc paths via `grep -vE '(\.md$|^\.planning/|^LICENSE$|^docs/|\.txt$)'`; an empty remainder is doc-only. **Conservative by construction** — any path not matching a doc pattern (source, test, workflow, `pyproject.toml`, unknown) counts as code, so a code change can never skip the security/test/docker jobs.
- Added the `just detect-code-changes` recipe (`[group('test')]`) delegating to the script (D-10).
- `ci.yml` `detect-changes`: added a `🔧 Setup Just` step, and replaced the inline `NON_MD_FILES=… grep -v '\.md$'` classifier + its `if` with a call that pipes `${CHANGED_FILES}` into `just detect-code-changes` and appends the result to `${GITHUB_OUTPUT}`. The SHA edge-case block and the `aggregate-results` job are unchanged (verified via `git diff`).

**Task 2 — regression tests (commit 6025e48):**
- Created `tests/shared/test_change_gate.py` (in the `shared` bucket so it is collected + covered). It runs the real `scripts/classify-changed-files.sh` over `subprocess.run` feeding crafted changed-file lists on stdin and asserts the printed `code-changed` value.
- Parametrised cases: all-`.planning/**` -> false; `LICENSE`-only -> false; `docs/…` + `.txt` -> false; markdown-only -> false; empty -> false; a bare `.py` -> true; `.github/workflows/ci.yml` -> true; `pyproject.toml` -> true.
- A dedicated non-parametrised `test_mixed_doc_and_code_is_conservative` asserts a mixed doc + `.py` list -> `code-changed=true` (the security property T-63-04-01 has an explicit positive test, so the classifier is not vacuously permissive).
- Locates the script via `Path(__file__).resolve().parents[2]` (repo root from `tests/shared/`); plain sync test, no DB fixture.

## Verification

- Classifier acceptance cases (all-doc/mixed/md-only/empty/bare-.py) return the expected `code-changed` value; `just detect-code-changes` produces byte-exact `code-changed=false`/`code-changed=true` (18 chars, no noise).
- `uv run pytest tests/shared/test_change_gate.py -x -q` -> 9 passed. With the partition guard: 12 passed.
- Hooks green: `shellcheck` + `shfmt` (script), `actionlint` + `check-github-workflows` (ci.yml), `ruff` + `ruff format` + `mypy` (test). Both commits passed the full pre-commit sweep with no `--no-verify`.
- `git diff .github/workflows/ci.yml` confirms only the `detect-changes` classification changed; the SHA edge-case block and `aggregate-results` (`if: always()`, `code-changed==false -> exit 0`) are byte-for-byte unchanged; no switch to `paths-ignore`.

## Skip-with-success contract (preserved)

A doc-only PR flows: `detect-changes` -> `code-changed=false` -> `test`/`security`/`docker` skipped -> `aggregate-results` (`if: always()`) hits the `code-changed==false` branch and `exit 0`, so the single stable required check reports SUCCESS (green, not pending/neutral) and the PR stays mergeable. This branch and job were not modified.

## Threat surface

Threat register mitigations all satisfied:
- T-63-04-01 (code change bypasses scans): conservative keep-only-non-doc classifier + explicit mixed doc+code positive test.
- T-63-04-02 (unmergeable required-check): `detect-changes` gate + `aggregate-results` `if: always()` + `exit 0` kept verbatim; no `paths-ignore`.
- T-63-04-03 (classifier drift): classifier is a versioned shellcheck-clean script under regression tests, not inline YAML shell.
- T-63-04-04 (fork-PR secret exposure): triggers/permissions unchanged (`pull_request`, not `pull_request_target`).
- T-63-04-SC (package installs): none added.

No new security-relevant surface introduced.

## Deviations from Plan

None — plan executed exactly as written. The `just test-bucket` / `just coverage-combine` recipes already existed (from 63-01); only `detect-code-changes` was added. `scripts/update-project.sh` needs no edit — `detect-code-changes` is a CI-only classifier, not part of the local lint/typecheck/test verify sweep (per the PATTERNS note).

## Notes for Downstream

- Adding a new documentation path type (e.g. `.rst` at repo root, a new top-level docs dir) means editing the single `grep -vE` pattern in `scripts/classify-changed-files.sh` and adding a case to `tests/shared/test_change_gate.py` — the classifier is the one place to change, and the test guards it.
- The required branch-protection check remains `aggregate-results`; this plan did not touch the required-check topology.

## Self-Check: PASSED

- Files: FOUND `scripts/classify-changed-files.sh`, FOUND `tests/shared/test_change_gate.py`, FOUND `.github/workflows/ci.yml`, FOUND `justfile`.
- Commits: FOUND `372609a` (Task 1), FOUND `6025e48` (Task 2).
