"""phaze-q78v2: mechanical guard that CI fails loudly on a stale `uv.lock`.

A dependabot PR that bumps only a version constraint in `pyproject.toml` is not
lock-synced by construction -- `uv lock` never runs as part of the dependency-bump
commit, so the PR merges with `pyproject.toml` and `uv.lock` disagreeing. Nothing
in CI failed on that until this fix: dependabot merge 6a127599 (PR #553) bumped the
`ruff` dev requirement while `uv.lock` still recorded the old constraint, and the
drift sat on `main` until it disarmed `check-fast`'s git-diff-based test selector --
the first `uv run` in a fresh checkout silently rewrote the one line in `uv.lock`
and left the tree dirty, which the selector reads as "everything changed" (or, per
the incident, as unrelated noise an unwary seat could sweep into its own commit).

The fix is a `uv lock --check` step in `code-quality.yml`, positioned BEFORE
`just setup` (`uv sync`). Position is load-bearing, not cosmetic: `uv sync` will
happily relock a stale `uv.lock` in place rather than failing, so a check placed
after it would pass against a lockfile the sync had already silently rewritten --
exactly the silent-rewrite failure mode this guard exists to turn into a loud one.
A pre-commit-only guard would not have caught the original incident either:
dependabot PRs run CI, they do not run a developer's local pre-commit hooks.

Deliberately parses the raw workflow YAML rather than shelling out to `uv` or
`gh` -- no network, no dependency on the checkout's own lock state, safe to run
in any environment.
"""

from __future__ import annotations

from pathlib import Path

import yaml


_REPO_ROOT = Path(__file__).resolve().parents[2]
_CODE_QUALITY_WORKFLOW_PATH = _REPO_ROOT / ".github" / "workflows" / "code-quality.yml"


def _code_quality_steps() -> list[dict[str, object]]:
    assert _CODE_QUALITY_WORKFLOW_PATH.exists(), f"missing {_CODE_QUALITY_WORKFLOW_PATH}"
    data = yaml.safe_load(_CODE_QUALITY_WORKFLOW_PATH.read_text(encoding="utf-8"))
    return list(data["jobs"]["code-quality"]["steps"])


def _step_index(steps: list[dict[str, object]], *, run_contains: str) -> int:
    matches = [i for i, step in enumerate(steps) if run_contains in str(step.get("run", ""))]
    assert len(matches) == 1, f"expected exactly one step with run containing {run_contains!r}, found {len(matches)}"
    return matches[0]


def test_code_quality_workflow_checks_uv_lock_is_in_sync() -> None:
    """`code-quality.yml` must run `uv lock --check` so a stale lock fails the PR."""
    steps = _code_quality_steps()
    index = _step_index(steps, run_contains="uv lock --check")
    step = steps[index]
    assert step.get("run", "").strip() == "uv lock --check", (
        f"the uv.lock sync-check step must run exactly `uv lock --check` (read-only -- must not itself relock); got {step.get('run')!r}"
    )


def test_uv_lock_check_runs_before_the_first_uv_sync() -> None:
    """The lock-sync check must run BEFORE `just setup` (`uv sync`), never after.

    `uv sync` silently relocks a stale `uv.lock` in place rather than failing, so a
    check placed after it would pass against a lockfile the sync had already
    rewritten -- the exact silent-rewrite failure mode phaze-q78v2 exists to catch.
    """
    steps = _code_quality_steps()
    check_index = _step_index(steps, run_contains="uv lock --check")
    setup_index = _step_index(steps, run_contains="just setup")
    assert check_index < setup_index, (
        "`uv lock --check` must run before `just setup` (uv sync) -- otherwise sync silently fixes the drift before the check ever sees it"
    )
