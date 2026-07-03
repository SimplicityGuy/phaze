#!/usr/bin/env bash
# Classify a newline-delimited list of changed file paths (read from stdin) as
# documentation-only or code, for the CI doc-only skip gate (CI-04, D-08/D-09).
#
# Prints exactly one line to stdout:
#   code-changed=false   every changed path is documentation (skip heavy jobs)
#   code-changed=true    at least one path is code / tests / workflow / config
#
# The output is intentionally in GitHub Actions `name=value` form so `ci.yml`'s
# detect-changes job can append it straight to ${GITHUB_OUTPUT}.
#
# CONSERVATIVE by construction (security mitigation T-63-04-01): only paths that
# clearly match a documentation pattern are treated as docs. ANYTHING else — a
# source file, a test, a workflow, pyproject.toml, or an unrecognised path — keeps
# code-changed=true, so a code change can never skip the security/test/docker jobs.
# A change set that mixes docs and code is therefore always classified as code.
#
# An EMPTY / absent file list is also treated as code-changed=true (fail safe):
# a spurious-empty diff (e.g. a broken diff base) must never silently skip CI.
# "code-changed=false" is reserved for the case where at least one path changed
# AND every changed path is documentation.
#
# Skippable (documentation) patterns:
#   *.md            any Markdown file, anywhere in the tree
#   .planning/**    GSD planning artifacts
#   LICENSE         the top-level licence file
#   docs/**         the documentation tree
#   *.txt           plain-text docs
#
# Invoked from CI via `just detect-code-changes` (D-10) and unit-tested by
# tests/shared/test_change_gate.py.
set -euo pipefail

# Read all of stdin once so we can distinguish "no changed files at all" (fail
# safe -> run everything) from "changed files, all documentation" (skip).
raw_input="$(cat)"

# Non-blank changed paths, if any. `|| true` keeps the pipeline alive under
# `set -o pipefail` when grep selects nothing (exit status 1) or stdin is empty.
changed_paths="$(printf '%s\n' "${raw_input}" | grep -v '^[[:space:]]*$' || true)"

if [[ -z "${changed_paths}" ]]; then
  # Empty / whitespace-only file list: fail safe, run the full pipeline. A
  # spurious-empty diff must never skip the security/test/docker jobs.
  echo "code-changed=true"
  exit 0
fi

# At least one path changed. Keep only the NON-documentation paths; `grep -vE`
# drops every line matching a doc pattern.
code_files="$(printf '%s\n' "${changed_paths}" | grep -vE '(\.md$|^\.planning/|^LICENSE$|^docs/|\.txt$)' || true)"

if [[ -z "${code_files}" ]]; then
  # Every changed path is documentation -> skip the heavy jobs.
  echo "code-changed=false"
else
  echo "code-changed=true"
fi
