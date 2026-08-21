"""phaze-b62ri: guards on the two ffmpeg facts that are still assertable in the tree.

**Most of what this file used to assert no longer exists, and the tests went with it.** An
earlier shape of this bead pinned ffmpeg with an explicit `ffmpeg=7:7.1.5-0+deb13u1` in each
Dockerfile, and this module guarded that: the two declarations agreeing, the major not drifting,
no ffmpeg-family package escaping the pin, no literal repeated at an install site. Those pins were
removed — an explicit apt version is worse than nothing, because it selects *away* from Debian's
security updates (see `docs/design/0013-ffmpeg-pin.md` §3). With no pin there is no duplication to
prove consistent and no version string to guard, so those tests were **deleted rather than
softened**. A test that no longer has a claim to make should not exist; keeping it as a vacuous
pass is worse than having no test, because it reads like coverage.

What survives is what the tree still genuinely asserts:

1. **The arm64 agent's base image is trixie.** After the pins were removed the base tag is the
   *entire* version-control mechanism — Debian locks the upstream MAJOR.MINOR line for a release,
   so `python:3.13-slim-trixie` is what makes that image ffmpeg 7.1.x instead of bookworm's 5.1.x.
   Reverting the base silently reverts the ffmpeg version, and nothing else in the tree notices.
2. **The CI/image ffmpeg divergence stays documented.** CI tests a pinned BtbN 8.1 build while the
   images ship Debian 7.1.x. That is a real gap with an open decision attached, and the failure
   mode is that it gets forgotten.

Deliberately parses raw text — no docker daemon, no network, runs anywhere.
"""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
AGENT_DOCKERFILE = REPO_ROOT / "Dockerfile.agent-arm64"
TESTS_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "tests.yml"


def _text(path: Path) -> str:
    assert path.exists(), f"{path.name} missing at {path}"
    return path.read_text()


def test_arm64_agent_base_is_trixie() -> None:
    """The agent's base image is the ffmpeg version control, not an incidental choice.

    bookworm serves ffmpeg 5.1.x and trixie serves 7.1.x. With no apt pin anywhere, the base tag
    is the only thing deciding which this image gets — and it decides it for the CLI *and* for the
    `libav*-dev` set the from-source essentia compiles against, since both come from one Debian
    source package. Reverting the base would put the agent two majors behind the app image again,
    which is the exact divergence this bead closed, and it would do so without any version string
    changing anywhere in the diff.
    """
    from_lines = [ln.strip() for ln in _text(AGENT_DOCKERFILE).splitlines() if ln.startswith("FROM ")]
    assert from_lines, "Dockerfile.agent-arm64 has no FROM line"
    assert all("python:3.13-slim-trixie" in ln for ln in from_lines), (
        f"Dockerfile.agent-arm64 must build every stage on python:3.13-slim-trixie; found {from_lines!r}. "
        "bookworm serves ffmpeg 5.1.x — reverting the base silently reverts the ffmpeg version "
        "and re-opens the app/agent split (phaze-b62ri)."
    )


def test_arm64_agent_stays_on_python_313() -> None:
    """Python 3.13 on the agent is deliberate and must survive the base move.

    TensorFlow ships no cp314 aarch64 wheel and dependabot PR #326 already proved 3.14 breaks this
    build. The trixie move changes the base's Debian suite, not its Python. The inconsistency with
    the repo's "3.14 exclusively" contract is intentional and documented in the Dockerfile — this
    guards against it being tidied away, which is the plausible mistake now that the base line has
    been shown to be safe to edit.
    """
    text = _text(AGENT_DOCKERFILE)
    assert "python:3.13-slim" in text, "Dockerfile.agent-arm64 must stay on Python 3.13 (no cp314 aarch64 TF wheel)"
    assert "python:3.14" not in text, "Dockerfile.agent-arm64 must NOT move to Python 3.14 — TF has no cp314 aarch64 wheel (PR #326 proved it breaks)"


def test_ci_image_ffmpeg_divergence_is_recorded() -> None:
    """CI and the images run different ffmpeg majors, and that must stay written down.

    CI downloads a pinned BtbN **8.1** build; the images ship Debian trixie's **7.1.x**. A CI that
    tests a different ffmpeg major than production ships is precisely the gap this bead is about,
    so it is not allowed to become implicit.

    Each required string is load-bearing and hard to satisfy by accident: the bead that owns the
    divergence, the version the images actually carry, and a pointer to where the open decision is
    argued. A looser check — "mentions 7.1 and some word like 'differs'" — was tried first and
    passed even after the explanation had been gutted, which is why it is not the check.

    This deliberately does NOT assert the two versions match. The split is a **settled operator
    decision** (``phaze-b62ri``, 2026-08-20), not an oversight awaiting cleanup: asked whether CI
    should move to 7.1.x, the
    operator declined, on the grounds that CI exercises ffmpeg only through the audio-extract path
    and that this is sufficiently isolated. A test asserting equality would encode the opposite of
    the decision. See ADR-0013 §7.
    """
    text = _text(TESTS_WORKFLOW)
    required = {
        "phaze-b62ri": "the bead that owns this divergence",
        "7.1": "the ffmpeg line the images actually ship, so both versions are visible here",
        "0013-ffmpeg-pin.md": "the decision record where the open CI question is argued",
    }
    missing = {k: why for k, why in required.items() if k not in text}
    assert not missing, (
        "tests.yml must keep explaining why CI's ffmpeg differs from what the images ship. Missing:\n"
        + "\n".join(f"  {k!r} — {why}" for k, why in missing.items())
        + "\nLeaving it undocumented is how the gap gets forgotten."
    )
