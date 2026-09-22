#!/usr/bin/env bash
# Fail LOUDLY when the checkout running a gate has been hollowed out (phaze-ofrxb).
#
# The failure this guards against: bh worktrees used to live under $TMPDIR, and macOS's
# com.apple.bsd.dirhelper (launchd, daily at 03:35, CLEAN_FILES_OLDER_THAN_DAYS=3) deletes
# every file there that has not been ACCESSED for three days. A multi-day seat then loses its
# untouched tracked files, its `.git` pointer file, and the module files inside `.venv` -- and
# the first symptom is a gate dying in ruff/mypy/pytest with `ModuleNotFoundError` or
# `ImportError`, which reads as a code regression and gets bounced as one. The seat's venv is
# hit FIRST and WORST because uv clones (APFS clonefile) each file out of ~/.cache/uv with the
# cache file's timestamps intact, so a freshly built venv can already be "stale" to dirhelper.
# The worktree root has since moved to a persistent path (~/.beadhive/worktrees), but a guard
# that names the real cause is cheap insurance against the next transferred belief about a
# directory's durability.
#
# Usage: scripts/worktree-integrity.sh [checkout-dir]   (default: current directory)
# Exit 0 when every check passes; exit 1 with a `worktree-integrity:` line per finding and the
# remedy. Deliberately runs with NO `uv run` and NO venv: when pyproject.toml or the venv is what
# is missing, anything that needs them cannot be the thing that reports it.
#
# Checks, in the order a hollowed seat fails them:
#   1. `.git` exists (file for a worktree, directory for the main clone).
#   2. `pyproject.toml` exists.
#   3. No tracked file is missing from disk (`git ls-files --deleted`).
#   4. If `.venv` exists: its python runs, and the gate's own imports (mypy, pytest, defusedxml)
#      resolve to files that exist -- a purged package keeps its directory and `__pycache__`, so
#      `import` can succeed with `__file__ = None` or fail with ImportError.
set -eu

root="${1:-$PWD}"
status=0
# Modules the per-bead gate itself needs; a hollowed venv shows up here before any test runs.
probe_modules="mypy pytest defusedxml"

finding() {
  printf 'worktree-integrity: %s\n' "$1" >&2
  status=1
}

if [ ! -e "$root/.git" ]; then
  finding "$root/.git is missing -- this checkout is no longer a git worktree. Re-provision it from the bead branch (bh work resume <id>); the branch is the durable artifact, the directory never was."
fi

if [ ! -f "$root/pyproject.toml" ]; then
  finding "$root/pyproject.toml is missing -- tracked files have been deleted from this checkout."
fi

if [ -e "$root/.git" ]; then
  deleted="$(git -C "$root" ls-files --deleted 2>/dev/null || true)"
  if [ -n "$deleted" ]; then
    count="$(printf '%s\n' "$deleted" | wc -l | tr -d ' ')"
    finding "$count tracked file(s) missing from disk (first few): $(printf '%s\n' "$deleted" | head -5 | tr '\n' ' ')-- restore with 'git checkout -- .' after confirming nothing here was an intended deletion."
  fi
fi

if [ -d "$root/.venv" ]; then
  py="$root/.venv/bin/python"
  if [ ! -x "$py" ]; then
    finding "$py is missing or not executable -- rebuild the venv: rm -rf .venv && uv sync"
  else
    # One interpreter call, one line per module: 'ok <name>' or 'missing <name>: <reason>'.
    probe_out="$(
      "$py" - "$probe_modules" <<'PY' 2>&1 || true
import importlib
import os
import sys

for name in sys.argv[1].split():
    try:
        module = importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001 - any failure to import is the finding
        print(f"missing {name}: {type(exc).__name__}: {exc}")
        continue
    path = getattr(module, "__file__", None)
    if not path or not os.path.exists(path):
        print(f"missing {name}: imported with __file__={path!r} (package directory hollowed)")
        continue
    print(f"ok {name}")
PY
    )"
    if printf '%s\n' "$probe_out" | grep -q '^missing '; then
      finding "$(printf '%s\n' "$probe_out" | grep '^missing ' | tr '\n' ';') -- the venv has been hollowed (files purged under it, not a code regression). Rebuild: rm -rf .venv && uv sync"
    elif ! printf '%s\n' "$probe_out" | grep -q '^ok '; then
      finding "$py could not run the import probe: $(printf '%s' "$probe_out" | head -3 | tr '\n' ' ') -- rebuild the venv: rm -rf .venv && uv sync"
    fi
  fi
fi

if [ "$status" -ne 0 ]; then
  printf 'worktree-integrity: FAILED for %s -- see phaze-ofrxb / docs/design/0016-transferred-model-verification.md §3.8\n' "$root" >&2
fi
exit "$status"
