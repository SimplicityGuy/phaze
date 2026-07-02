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

# Keep only the NON-documentation paths. `grep -vE` drops every line matching a doc
# pattern; the second `grep` strips blank lines (an empty stdin, or a trailing
# newline, must not count as a code path). `|| true` keeps the pipeline alive under
# `set -o pipefail` when grep selects nothing (exit status 1) or stdin is empty.
code_files="$(grep -vE '(\.md$|^\.planning/|^LICENSE$|^docs/|\.txt$)' | grep -v '^[[:space:]]*$' || true)"

if [[ -z "${code_files}" ]]; then
  echo "code-changed=false"
else
  echo "code-changed=true"
fi
