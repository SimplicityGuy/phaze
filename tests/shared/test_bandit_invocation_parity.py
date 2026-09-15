"""phaze-stv2i: mechanical guard that pre-commit and CI run the SAME bandit invocation.

Measured 2026-09-10: bandit's `-x`/`--exclude` matches path SUBSTRINGS (glob patterns)
against whatever targets it is given -- including a single explicit file path, not just a
directory being walked. The `pre-commit/bandit` hook has no `-r` in its own manifest and
passes each changed Python file individually (`types: [python]`, no `pass_filenames:
false`), so the old local config's `-x tests,services` excluded every file whose path
contained the substring "services" -- not only the top-level `services/` prototype
directory it was meant to skip (see `pyproject.toml`'s `mypy` `exclude` and
`per-file-ignores`, both anchored with `^`/`services/**` at the repo root), but also every
file under `src/phaze/services/`. CI's invocation, `just bandit`
(`uv run bandit -r src/ -x tests -s B608`, run from `.github/workflows/security.yml`'s
"Run bandit" step), has no "services" exclusion at all and does scan that tree. Result:
phaze-08fom's B101 assert in `src/phaze/services/set_glyph_colors.py` passed every local
gate and the submit validation, and failed only on `main`'s post-merge CI.

The fix makes drift structurally impossible rather than merely re-synchronizing the two
exclusion lists by hand: the pre-commit hook is now a `local`/`system` hook whose `entry`
is literally `just bandit` -- the identical recipe CI runs -- so there is exactly one
bandit invocation in the repository, not two lists that can silently diverge again.

Deliberately parses `.pre-commit-config.yaml` and `justfile` as text/YAML rather than
shelling out to `bandit` or `pre-commit run` -- no network, no bandit dependency, safe to
run in any environment. `test_bandit_hook_runs_via_just_bandit_recipe` is the load-bearing
assertion; the others document the recipe body and the removal of the old drift-prone
`PyCQA/bandit` repo hook.
"""

from __future__ import annotations

from pathlib import Path
import re

import yaml


_REPO_ROOT = Path(__file__).resolve().parents[2]
_PRECOMMIT_CONFIG_PATH = _REPO_ROOT / ".pre-commit-config.yaml"
_JUSTFILE = _REPO_ROOT / "justfile"
_SECURITY_WORKFLOW_PATH = _REPO_ROOT / ".github" / "workflows" / "security.yml"


def _precommit_config() -> dict[str, object]:
    assert _PRECOMMIT_CONFIG_PATH.exists(), f"missing {_PRECOMMIT_CONFIG_PATH}"
    return yaml.safe_load(_PRECOMMIT_CONFIG_PATH.read_text(encoding="utf-8"))


def _local_hooks() -> list[dict[str, object]]:
    for repo in _precommit_config()["repos"]:
        if repo.get("repo") == "local":
            return list(repo["hooks"])
    raise AssertionError("no `repo: local` section found in .pre-commit-config.yaml")


def _bandit_local_hook() -> dict[str, object]:
    hooks = [h for h in _local_hooks() if h.get("id") == "bandit"]
    assert len(hooks) == 1, f"expected exactly one local `bandit` hook, found {len(hooks)}"
    return hooks[0]


def _justfile_bandit_recipe_body() -> str:
    text = _JUSTFILE.read_text(encoding="utf-8")
    match = re.search(r"^bandit:\n((?:[ \t]+.*\n?)*)", text, re.MULTILINE)
    assert match is not None, "no top-level `bandit:` recipe found in justfile"
    return match.group(1)


def test_bandit_hook_runs_via_just_bandit_recipe() -> None:
    """The pre-commit bandit hook must be `entry: just bandit` -- ONE invocation, not two.

    This is what makes drift structurally impossible rather than just re-synchronized:
    the pre-commit hook and the CI step (`security.yml`'s "Run bandit" step, `run: just
    bandit`) both resolve to the exact same justfile recipe body, so there is nothing left
    to keep in sync by hand.
    """
    hook = _bandit_local_hook()
    assert hook.get("entry") == "just bandit", (
        f"the local bandit hook must run `just bandit` (the same recipe CI uses), not a second, "
        f"independently-flagged invocation; got entry={hook.get('entry')!r}"
    )
    assert hook.get("language") == "system"
    assert hook.get("pass_filenames") is False, (
        "pass_filenames must be false: `just bandit` always targets `-r src/` itself, so pre-commit "
        "must not additionally pass a partial file list that could under-scan a partially-staged commit"
    )


def test_bandit_hook_is_no_longer_a_frozen_pycqa_repo_hook() -> None:
    """The old `PyCQA/bandit` repo hook (with its own, independently-flagged `-x`) must be gone.

    Its `-x tests,services` argument was the actual bug: bandit's `-x` matches path
    SUBSTRINGS, so "services" silently excluded all of `src/phaze/services/**`, not only the
    top-level `services/` prototype directory it was meant for.
    """
    repos = {repo.get("repo") for repo in _precommit_config()["repos"]}
    assert "https://github.com/PyCQA/bandit" not in repos, (
        "a frozen `PyCQA/bandit` repo hook re-appeared alongside the local `just bandit` hook -- "
        "that reintroduces exactly the two-invocations-that-can-drift shape this guard exists to prevent"
    )


def test_ci_bandit_step_runs_the_same_just_recipe() -> None:
    """`security.yml`'s bandit step must also invoke `just bandit`, not a raw `bandit` command."""
    data = yaml.safe_load(_SECURITY_WORKFLOW_PATH.read_text(encoding="utf-8"))
    steps = data["jobs"]["python-security"]["steps"]
    bandit_steps = [s for s in steps if "bandit" in s.get("run", "").lower()]
    assert len(bandit_steps) == 1, f"expected exactly one bandit step in security.yml, found {len(bandit_steps)}"
    (bandit_step,) = bandit_steps
    assert bandit_step.get("run", "").strip() == "just bandit", (
        f"security.yml's bandit step must run exactly `just bandit`; got {bandit_step.get('run')!r}"
    )


def test_just_bandit_recipe_scans_src_phaze_services() -> None:
    """The `bandit` justfile recipe's exclusion must not re-introduce a "services" substring match.

    `-r src/` already confines the scan to `src/`, so a bare `-x tests` cannot spuriously
    exclude `src/phaze/services/**` -- unlike the old `-x tests,services` shape, where the
    substring "services" matched that path too. This pins the recipe body against a
    regression that re-adds "services" to the exclude list.
    """
    body = _justfile_bandit_recipe_body()
    assert "uv run bandit -r src/ -x tests -s B608" in body, f"unexpected `bandit` recipe body:\n{body}"
    assert "services" not in body, (
        f"the `bandit` recipe must not exclude anything matching the substring 'services' -- that "
        f"would silently re-exclude all of src/phaze/services/** (phaze-stv2i); got:\n{body}"
    )
