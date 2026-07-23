"""Static deployment guard: the audfprint sidecar clones a SHA-pinned upstream commit (phaze-vvf0).

`Dockerfile.audfprint` used to `git clone --depth 1` dpwe/audfprint's unpinned default-branch
HEAD, so the artifact this image is built from could change underneath us between two builds of
the same phaze release with no diff, no checksum, and no reproducibility -- unlike every other
upstream fetch in the repo (`Dockerfile.panako`'s `PANAKO_REF`, `Dockerfile.agent-arm64`'s
`ESSENTIA_SHA`, SHA-frozen pre-commit hooks). These text-grep guards lock the pin against a
regression back to an unpinned `master`/HEAD clone.
"""

from pathlib import Path
import re


REPO_ROOT = Path(__file__).resolve().parents[3]
AUDFPRINT_DOCKERFILE = REPO_ROOT / "services" / "audfprint" / "Dockerfile.audfprint"

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


def test_audfprint_clone_is_pinned_to_a_full_commit_sha() -> None:
    assert AUDFPRINT_DOCKERFILE.exists(), f"sidecar Dockerfile missing at {AUDFPRINT_DOCKERFILE}"
    text = AUDFPRINT_DOCKERFILE.read_text()

    match = re.search(r"^ARG\s+AUDFPRINT_SHA=(\S+)\s*$", text, re.MULTILINE)
    assert match, (
        f"{AUDFPRINT_DOCKERFILE.name} must declare an ARG AUDFPRINT_SHA=<commit> pin, mirroring Dockerfile.panako's PANAKO_REF -- phaze-vvf0."
    )
    sha = match.group(1)
    assert _FULL_SHA.match(sha), f"AUDFPRINT_SHA must be a full 40-character commit SHA, got: {sha!r}"

    # The clone must check out that exact pinned SHA (detached), not float on whatever branch HEAD
    # currently points to.
    assert re.search(r'git\s+-C\s+/app/audfprint\s+checkout\s+--detach\s+"\$\{AUDFPRINT_SHA\}"', text), (
        f'{AUDFPRINT_DOCKERFILE.name} must `git checkout --detach "${{AUDFPRINT_SHA}}"` the pinned commit after cloning -- phaze-vvf0.'
    )


def test_audfprint_clone_is_not_a_bare_depth_1_default_branch_clone() -> None:
    """Regression guard for the exact defect: `git clone --depth 1 <url>` with no ref/SHA argument
    silently follows whatever the upstream default branch currently points to."""
    text = AUDFPRINT_DOCKERFILE.read_text()
    assert not re.search(r"git\s+clone\s+--depth\s+1\s+https://github\.com/dpwe/audfprint\.git\s+/app/audfprint\s*$", text, re.MULTILINE), (
        f"{AUDFPRINT_DOCKERFILE.name} must not clone dpwe/audfprint's unpinned default-branch HEAD "
        f"-- pin to an explicit commit SHA via ARG AUDFPRINT_SHA, mirroring Dockerfile.panako "
        f"(phaze-vvf0)."
    )
