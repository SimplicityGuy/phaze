#!/usr/bin/env bash
# Derive the RAW seat name `just test-validate` provisions for itself when the caller exported no
# TEST_DATABASE_URL (phaze-bk9el.23). Prints one name on stdout and nothing else.
#
# THE DEFECT THIS CLOSES. `test-validate` -- what `just check`, `just check-all`, and therefore
# `bh work check` / `bh work submit` all run -- used to fall back to the SHARED `phaze_test`
# database and Redis logical DB 0 when TEST_DATABASE_URL was unset. Every concurrent seat that
# forgot to export its own rig landed on the same pair, where phaze-ieqg's session advisory lock
# refuses the second pytest: a RED RUN THAT PASSES ON ISOLATED RE-RUN, which CLAUDE.md names as
# the worst possible failure shape because it trains reviewers to dismiss reds.
#
# WHY DERIVE RATHER THAN REFUSE. Refusing outright when TEST_DATABASE_URL is unset was proposed
# and rejected: the auto-provision exists deliberately so a FRESH WORKTREE WITH NO SEAT still
# works. A hard refusal fixes the concurrent case by breaking the solo case. So the fallback
# stays a fallback -- it just stops being a SHARED one.
#
# WHAT THE NAME IS MADE OF, and why each part is there:
#
#   auto-<label>-<pathhash>
#
#   * `auto-` marks the seat as gate-provisioned rather than operator-provisioned, so
#     `just test-db-seats` shows at a glance which seats nobody typed a `test-db-for` for.
#     It also guarantees the leading-lowercase-letter that `derive-seat-name.sh` requires,
#     whatever the branch is called.
#   * `<label>` is the branch name (or, detached, the worktree directory name), sanitized and
#     truncated. Readability only -- it is what makes `just test-db-seats` legible. It carries
#     no uniqueness guarantee and is not relied on for any.
#   * `<pathhash>` is a short digest of the ABSOLUTE worktree root. This is the part that
#     actually guarantees two worktrees never share a seat. Branch names are unique per worktree
#     within one repository (git refuses to check the same branch out twice), but two CLONES can
#     both sit on `main`, and a detached HEAD has no branch at all. The path is unique in every
#     one of those cases, and it is STABLE across re-runs in the same worktree, which is what
#     makes provisioning idempotent -- a fresh name per invocation would mint a new database and
#     burn a new Redis index on every gate run.
#
# The result is then normalized by `derive-seat-name.sh` in the usual way (hyphens to
# underscores plus its own hash of this raw name), so the final identifier looks like
# `auto_wt_bead_issue_phaze_ab12_3f9c1d_a1b2c3d4`.
#
# Usage: derive-validate-seat-name.sh [worktree-root]
set -euo pipefail

root="${1:-}"
if [ -z "$root" ]; then
  # --show-toplevel is the WORKTREE root, not the common .git dir, which is what we want: each
  # linked worktree gets its own.
  root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
fi
# Not a git checkout at all (a tarball export, a container COPY). $PWD is still stable and still
# distinguishes concurrent trees, so degrade to it rather than failing the gate.
[ -n "$root" ] || root="$PWD"
root="$(cd -- "$root" && pwd -P)"

label="$(git -C "$root" symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
[ -n "$label" ] || label="$(basename -- "$root")"

# Fold to the alphabet `derive-seat-name.sh` accepts: lowercase letters, digits, '-' and '_'.
# Everything else (slashes in `wt/bead/issue/...`, dots, anything exotic) becomes '-'; runs are
# collapsed and the ends trimmed so the name never has a leading/trailing or doubled separator.
label="$(printf '%s' "$label" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9_-' '-')"
label="$(printf '%s' "$label" | sed -e 's/-\{2,\}/-/g' -e 's/^-//' -e 's/-$//')"
# Truncated for legibility only. `derive-seat-name.sh` budgets the Postgres 63-byte identifier
# cap itself, so this cannot be the thing that overflows -- but an 18-char label keeps the derived
# identifier readable in `just test-db-seats` instead of being truncated to noise there.
label="${label:0:18}"
label="$(printf '%s' "$label" | sed -e 's/-$//')"
[ -n "$label" ] || label="wt"

if command -v sha256sum >/dev/null 2>&1; then
  path_hash="$(printf '%s' "$root" | sha256sum | cut -c1-6)"
elif command -v shasum >/dev/null 2>&1; then
  path_hash="$(printf '%s' "$root" | shasum -a 256 | cut -c1-6)"
else
  echo "❌ neither sha256sum nor shasum available to derive a per-worktree seat name" >&2
  exit 1
fi

printf 'auto-%s-%s\n' "$label" "$path_hash"
