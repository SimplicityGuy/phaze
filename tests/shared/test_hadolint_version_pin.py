"""phaze-ny3du: mechanical guard against the hadolint version pin diverging.

hadolint is pinned in two places, only one of which is authoritative:

1. ``.pre-commit-config.yaml``'s ``hadolint`` repo ``rev:`` (a ``# frozen: vX.Y.Z``
   comment names the release the SHA corresponds to).
2. ``.github/workflows/code-quality.yml``'s ``hadolint:`` value on the
   ``alexellis/arkade-get`` step that installs the binary.

The pre-commit hook is a ``language: system`` hook: it lints with whatever
``hadolint`` binary is on ``PATH``, so (1) pins the hook DEFINITION, not the binary
that actually runs. CI must independently install a binary at the SAME version via
(2), or a developer running the pinned-matching version locally and CI silently lint
Dockerfiles with two different hadolint releases -- rule-behavior deltas between
releases then split local and CI results.

This is a **recurrence**, not a first offense: phaze-b67d (hadolint 2.15.0 promoting
DL3066 reddened every open PR for ten hours) was fixed by pinning (2), and then a
later dependency refresh (635ff6c) bumped (1) to v2.15.1 without touching (2) --
exactly the split this test exists to catch mechanically instead of by luck.

Deliberately parses raw text/YAML rather than shelling out to `just`/`pre-commit` --
no network, no hadolint binary dependency, safe to run in any environment.
"""

from __future__ import annotations

from pathlib import Path
import re

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
PRECOMMIT_CONFIG_PATH = REPO_ROOT / ".pre-commit-config.yaml"
CODE_QUALITY_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "code-quality.yml"

# `rev: <40-hex-sha>  # frozen: vX.Y.Z` on the hadolint repo entry.
_FROZEN_REV_COMMENT_RE = re.compile(r"^\s*rev:\s*[0-9a-f]{40}\s*#\s*frozen:\s*(v[0-9]+\.[0-9]+\.[0-9]+)\s*$", re.MULTILINE)


def _precommit_config_text() -> str:
    assert PRECOMMIT_CONFIG_PATH.exists(), f".pre-commit-config.yaml missing at {PRECOMMIT_CONFIG_PATH}"
    return PRECOMMIT_CONFIG_PATH.read_text(encoding="utf-8")


def _hadolint_repo_block(text: str) -> str:
    """Return the YAML block for the `hadolint/hadolint` repo entry."""
    match = re.search(r"- repo: https://github\.com/hadolint/hadolint\n((?:[ \t]+.*\n?)*)", text)
    assert match is not None, "no `- repo: https://github.com/hadolint/hadolint` entry found in .pre-commit-config.yaml"
    return match.group(1)


def _precommit_hadolint_version() -> str:
    """The `# frozen: vX.Y.Z` version comment on the hadolint repo's `rev:` line."""
    block = _hadolint_repo_block(_precommit_config_text())
    match = _FROZEN_REV_COMMENT_RE.search(block)
    assert match, f"hadolint repo entry must pin `rev:` with a `# frozen: vX.Y.Z` comment; got:\n{block}"
    return match.group(1)


def _code_quality_hadolint_version() -> str:
    assert CODE_QUALITY_WORKFLOW_PATH.exists(), f"code-quality.yml missing at {CODE_QUALITY_WORKFLOW_PATH}"
    data = yaml.safe_load(CODE_QUALITY_WORKFLOW_PATH.read_text(encoding="utf-8"))
    steps = data["jobs"]["code-quality"]["steps"]
    arkade_steps = [s for s in steps if "arkade-get" in s.get("uses", "")]
    assert len(arkade_steps) == 1, f"expected exactly one arkade-get step in code-quality.yml, found {len(arkade_steps)}"
    (arkade_step,) = arkade_steps
    version = arkade_step.get("with", {}).get("hadolint")
    assert isinstance(version, str) and version.startswith("v"), (
        f"code-quality.yml's arkade-get `hadolint:` value must be a `vX.Y.Z` string; got {version!r}"
    )
    return version


def test_precommit_config_pins_hadolint_with_a_frozen_version_comment() -> None:
    """`.pre-commit-config.yaml` must name the hadolint release its frozen rev corresponds to."""
    version = _precommit_hadolint_version()
    assert re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", version), version


def test_code_quality_workflow_installs_a_pinned_hadolint_version() -> None:
    """`code-quality.yml` must install a pinned (non-`latest`) hadolint release."""
    version = _code_quality_hadolint_version()
    assert version != "latest", "code-quality.yml must pin an exact hadolint version, never `latest` (phaze-b67d)"
    assert re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", version), version


def test_hadolint_version_pins_agree_across_precommit_and_ci() -> None:
    """The pre-commit frozen-rev comment and the CI-installed binary must name the same release.

    This is the mechanical guard for the invariant both files already state in prose:
    a version bump touching only one side (phaze-ny3du: a pre-commit `rev:` bump to
    v2.15.1 that never touched code-quality.yml) now fails loudly here instead of
    silently linting Dockerfiles with two different hadolint releases in local vs. CI.
    """
    precommit_version = _precommit_hadolint_version()
    workflow_version = _code_quality_hadolint_version()

    assert precommit_version == workflow_version, (
        "hadolint version pin has diverged between .pre-commit-config.yaml and code-quality.yml:\n"
        f"  .pre-commit-config.yaml (# frozen: comment):        {precommit_version!r}\n"
        f"  code-quality.yml (arkade-get `hadolint:`):          {workflow_version!r}\n"
        "Bump BOTH together (this is phaze-b67d / phaze-ny3du recurring a third time if it fires)."
    )


def test_code_quality_workflow_documents_the_system_hook_constraint() -> None:
    """code-quality.yml must comment on why the arkade-installed binary has to match the pin.

    A comment can be deleted silently by a future edit, so pin the assertion to durable
    phrasing (the `system` hook mechanism) rather than exact wording.
    """
    text = CODE_QUALITY_WORKFLOW_PATH.read_text(encoding="utf-8")
    lower = text.lower()
    assert "system" in lower and "hadolint" in lower, (
        "code-quality.yml must document the `system`-hook / PATH-binary hadolint constraint near the arkade-get step"
    )
