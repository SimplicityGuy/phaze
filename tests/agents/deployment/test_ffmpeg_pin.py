"""phaze-b62ri: mechanical guard that every image's ffmpeg stays on one pinned release line.

``ffprobe`` is the sole authority on whether a file has an audio stream and which of several
tracks is the container's default (``services/video_audio.py``, phaze-3ea41), and it supplies the
duration that bounds every analysis window (``services/analysis.py::_probe_duration_sec``, D-10).
Its version is therefore a correctness input, not a packaging detail.

**What is pinned, and why it is more than the CLI.** The target is one ffmpeg release line across
every surface, because the CLI and the analysis library sit on either side of a live
producer/consumer seam: for video containers the CLI extracts an audio artifact that essentia's
libav then decodes. On amd64 essentia comes from a wheel that *statically links ffmpeg 7.1*, so
7.1.5 is what makes both sides agree. On arm64 essentia is compiled from source against the
system ``libav*-dev`` packages, so those are driven from the same version string as the CLI.

The pin is declared in ``Dockerfile`` and ``Dockerfile.agent-arm64``; ``Dockerfile.job`` inherits
it through ``FROM ${BASE_IMAGE}`` and must never install its own. Docker has no include directive,
so the two declarations are irreducible; this test is what replaces the single source of truth.

Deliberately parses raw text — no docker daemon, no network, runs anywhere.
"""

from pathlib import Path
import re


REPO_ROOT = Path(__file__).resolve().parents[3]
APP_DOCKERFILE = REPO_ROOT / "Dockerfile"
AGENT_DOCKERFILE = REPO_ROOT / "Dockerfile.agent-arm64"
JOB_DOCKERFILE = REPO_ROOT / "Dockerfile.job"
TESTS_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "tests.yml"

# The operator-decided target line (bead phaze-b62ri, rescoped 2026-08-20). Moving off ffmpeg 7
# means re-running the probe-surface and real-consumer comparisons in docs/design/0013-ffmpeg-pin.md
# against the new major, and re-checking it against whatever ffmpeg the essentia wheel links.
EXPECTED_MAJOR = "7"

# `7:7.1.5-0+deb13u1` — Debian epoch:upstream-revision.
_ARG_RE = re.compile(r"^ARG FFMPEG_APT_VERSION=(\S+)\s*$", re.MULTILINE)
_APT_VERSION_RE = re.compile(r"^(\d+):(\d+)\.(\d+)")
# Every apt package whose version must follow the ffmpeg pin: the CLI plus the libav/libsw family.
_FFMPEG_FAMILY_RE = re.compile(r"^(ffmpeg|libav\w*|libsw\w*|libpostproc\w*)(?:-dev)?\d*$")


def _dockerfile_text(path: Path) -> str:
    assert path.exists(), f"{path.name} missing at {path}"
    return path.read_text()


def _declared_pin(path: Path) -> str:
    """The single ``ARG FFMPEG_APT_VERSION=<version>`` default in one Dockerfile."""
    matches = _ARG_RE.findall(_dockerfile_text(path))
    assert len(matches) == 1, (
        f"{path.name} must declare exactly one `ARG FFMPEG_APT_VERSION=<version>` default; found {matches!r}. "
        "One declaration per file is what makes the pin greppable and the cross-file check meaningful."
    )
    return matches[0]


def _apt_install_tokens(path: Path) -> list[str]:
    """Every package token any ``apt-get install`` in this Dockerfile names.

    Continuations are joined first, quotes stripped, flags dropped. Tokens keep any ``=<version>``
    suffix so the caller can tell a pinned package from an unpinned one.
    """
    text = _dockerfile_text(path).replace("\\\n", " ")
    tokens: list[str] = []
    for line in text.splitlines():
        # Skip comments FIRST. The Dockerfiles' own prose explains `apt-get install
        # ffmpeg=<version>`, and a comment-blind scan reads that as a real install line —
        # which is how this parser first reported `ffmpeg=<version>` as an installed package.
        if line.lstrip().startswith("#"):
            continue
        if "apt-get install" not in line:
            continue
        tail = re.split(r"&&|\|\||;", line.split("apt-get install", 1)[1])[0]
        tokens.extend(tok.strip('"').strip("'") for tok in tail.split() if not tok.startswith("-"))
    return tokens


def test_both_images_declare_the_same_pin() -> None:
    """The app and agent images must name one ffmpeg release line, not two.

    Before this bead they ran different MAJORS — 7.1.5 on the app image (trixie) and 5.1.9 on the
    agent (bookworm) — while the tech-stack table claimed "8.x" for both. That divergence, on the
    path phaze-3ea41 made load-bearing, is what the bead exists to close.
    """
    app, agent = _declared_pin(APP_DOCKERFILE), _declared_pin(AGENT_DOCKERFILE)
    assert app == agent, (
        f"the ffmpeg pin has diverged between the images:\n  Dockerfile:             {app!r}\n"
        f"  Dockerfile.agent-arm64: {agent!r}\nBoth must name one release line; bump them together."
    )


def test_pinned_major_has_not_drifted() -> None:
    """ffmpeg 7 is a decision, not an incidental value.

    7.1 is what the essentia-tensorflow wheel statically links, so it is the version that keeps the
    CLI and the analysis library on one line. A bump to 8.x would reintroduce exactly the skew this
    bead removed — silently, since nothing else in the tree would notice.
    """
    pin = _declared_pin(APP_DOCKERFILE)
    match = _APT_VERSION_RE.match(pin)
    assert match, f"FFMPEG_APT_VERSION={pin!r} is not a Debian `epoch:major.minor…` version"
    major = match.group(2)
    assert major == EXPECTED_MAJOR, (
        f"pinned ffmpeg major moved from {EXPECTED_MAJOR} to {major} (FFMPEG_APT_VERSION={pin!r}). "
        "Re-run the probe-surface and real-consumer comparisons in docs/design/0013-ffmpeg-pin.md "
        "against the new major, AND re-check it against the ffmpeg the essentia wheel links "
        "(ADR-0013 §2) — the two must stay on one line. Then update EXPECTED_MAJOR here."
    )


def test_every_ffmpeg_family_package_is_pinned() -> None:
    """No ffmpeg/libav package may be apt-installed unpinned, in any image.

    This is the actual regression guard. A future edit adding a bare ``libavfilter-dev`` to the
    agent's build deps would compile essentia against an unpinned library while the CLI stayed
    pinned — the CLI-vs-library skew this bead removed, reintroduced through the back door and
    invisible in a green build.
    """
    for path in (APP_DOCKERFILE, AGENT_DOCKERFILE, JOB_DOCKERFILE):
        unpinned = [tok for tok in _apt_install_tokens(path) if _FFMPEG_FAMILY_RE.match(tok)]
        assert not unpinned, (
            f"{path.name} apt-installs ffmpeg-family package(s) without a version: {unpinned!r}. "
            'Pin them with "=${FFMPEG_APT_VERSION}" so the CLI and the libav set cannot diverge.'
        )


def test_pinned_packages_use_the_arg_not_a_literal() -> None:
    """Pinned ffmpeg-family packages must interpolate the ARG, never repeat the version.

    A literal repeated at the install site is a second copy of the pin inside the same file, which
    the cross-file check above cannot see and which a bump silently leaves behind.
    """
    for path in (APP_DOCKERFILE, AGENT_DOCKERFILE):
        pin = _declared_pin(path)
        offenders = [tok for tok in _apt_install_tokens(path) if tok.endswith(f"={pin}")]
        assert not offenders, (
            f"{path.name} repeats the literal version at an install site: {offenders!r}. "
            'Use "=${FFMPEG_APT_VERSION}" so the ARG stays the only declaration.'
        )


def test_job_image_installs_no_ffmpeg_of_its_own() -> None:
    """Dockerfile.job must inherit the pin, never re-declare it.

    It is ``FROM ${BASE_IMAGE}`` (the api image) and has no apt layer at all, so it already runs the
    pinned binary. Adding one here would create a second copy that drifts independently — and would
    do so invisibly, because the image would still work.
    """
    tokens = _apt_install_tokens(JOB_DOCKERFILE)
    assert not tokens, (
        f"Dockerfile.job now apt-installs {tokens!r}. It must stay dependency-free and inherit the "
        "api image's pinned ffmpeg through FROM (phaze-b62ri)."
    )


def test_arm64_agent_base_is_trixie() -> None:
    """The agent's base image is load-bearing for the pin, not incidental.

    bookworm cannot serve 7.1.5 at all — its only ffmpeg is 7:5.1.9-0+deb12u1 — so reverting the
    base would make the pinned install fail outright. That is the loud failure we want, but the
    reason belongs in a test rather than only in a comment, because "bump the base back" looks
    like an unrelated change until it breaks.
    """
    from_lines = [ln.strip() for ln in _dockerfile_text(AGENT_DOCKERFILE).splitlines() if ln.startswith("FROM ")]
    assert from_lines, "Dockerfile.agent-arm64 has no FROM line"
    assert all("python:3.13-slim-trixie" in ln for ln in from_lines), (
        f"Dockerfile.agent-arm64 must build every stage on python:3.13-slim-trixie; found {from_lines!r}. "
        "bookworm serves only ffmpeg 7:5.1.9-0+deb12u1 and cannot satisfy FFMPEG_APT_VERSION (phaze-b62ri)."
    )
    # Deliberately scoped to FROM lines: the file also CITES `3.14-slim-bookworm` when recording
    # what dependabot PR #326 proposed and why it failed. That citation is accurate history and
    # must survive — a blanket "bookworm" ban would delete the evidence for the Python 3.13 pin.


def test_arm64_agent_stays_on_python_313() -> None:
    """Python 3.13 on the agent is deliberate and must survive the base move.

    TensorFlow ships no cp314 aarch64 wheel and dependabot PR #326 already proved 3.14 breaks this
    build. The trixie move changes the base's Debian suite, not its Python — and the inconsistency
    with the repo's 3.14 contract is intentional, documented in the Dockerfile, and not to be tidied.
    """
    text = _dockerfile_text(AGENT_DOCKERFILE)
    assert "python:3.13-slim" in text, "Dockerfile.agent-arm64 must stay on Python 3.13 (no cp314 aarch64 TF wheel)"
    assert "python:3.14" not in text, "Dockerfile.agent-arm64 must NOT move to Python 3.14 — TF has no cp314 aarch64 wheel (PR #326 proved it breaks)"


def test_ci_image_ffmpeg_divergence_is_recorded() -> None:
    """CI and the images run different ffmpeg majors, and that must stay written down.

    CI downloads a pinned BtbN **8.1** build; the images now ship Debian **7.1.5**. A CI that tests
    a different ffmpeg major than production ships is precisely the gap this bead is about, so it is
    not allowed to be implicit. This asserts the workflow still explains the split; it deliberately
    does NOT assert the two versions match, because the operator DECIDED on 2026-08-20 that they
    should not — CI stays on 8.1.x, the containers on 7.1.5, on the verified grounds that CI's only
    real-binary ffmpeg consumer is the extract path (ADR-0013 §8). A future test asserting the two
    agree would encode the opposite of the decision.
    """
    text = _dockerfile_text(TESTS_WORKFLOW)
    # Each of these is load-bearing and hard to satisfy by accident: the bead that owns the
    # divergence, the EXACT pin the images carry (so a reader sees both numbers side by side),
    # and a pointer to where the open decision is argued. A looser check — "mentions 7.1.5 and
    # some word like 'differs'" — passed even after the explanation was gutted, which is why it
    # is not the check.
    required = {
        "phaze-b62ri": "the bead that owns this divergence",
        "7:7.1.5-0+deb13u1": "the exact pin the images carry, so both versions are visible here",
        "0013-ffmpeg-pin.md": "the decision record where the open CI question is argued",
    }
    missing = {k: why for k, why in required.items() if k not in text}
    assert not missing, (
        "tests.yml must keep explaining why CI's ffmpeg differs from what the images ship. Missing:\n"
        + "\n".join(f"  {k!r} — {why}" for k, why in missing.items())
        + "\nCI testing a different ffmpeg major than production ships is the gap phaze-b62ri exists to "
        "close; leaving it undocumented is how it gets forgotten."
    )
