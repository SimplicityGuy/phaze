"""Static deployment guards for the 1001Tracklists render worker image (phaze-fq9h.5).

These are text/structural checks against ``Dockerfile`` only — no live docker build (that is
covered by CI's ``docker-validate.yml``, which builds this exact Dockerfile and hadolint-lints
it). What they prove:

a. Xvfb is installed — the virtual display `XvfbDisplay` (services/tracklist_render.py) needs
   to run the render browser headful on a screenless worker.
b. Patchright's real Chrome channel is installed (`patchright install ... chrome`), matching the
   `tracklist_render_browser_channel` default of `"chrome"` in config.py.
c. `PLAYWRIGHT_BROWSERS_PATH` is pinned to a fixed, non-default path — Patchright inherits
   Playwright's cache layout, and the install runs as root while the browser later launches as
   the unprivileged `phaze` user, so a per-user cache default would leave the browser undiscoverable.
d. The Chrome install happens BEFORE the `USER phaze` switch (needs root for `--with-deps` apt-get).
"""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE_PATH = REPO_ROOT / "Dockerfile"


def _dockerfile_text() -> str:
    assert DOCKERFILE_PATH.exists(), f"Dockerfile missing at {DOCKERFILE_PATH}"
    return DOCKERFILE_PATH.read_text()


def test_dockerfile_installs_xvfb() -> None:
    """Guard (a): Xvfb must be apt-installed for the headful render browser's virtual display."""
    text = _dockerfile_text()
    assert "xvfb" in text.lower(), "Dockerfile must apt-install `xvfb` for the tracklist render engine's virtual display (phaze-fq9h.5)."
    # The existing native-deps liveness assertion (`command -v rsync ssh`) is extended to Xvfb
    # so a future slimming pass fails the build instead of crash-looping the render engine at runtime.
    assert "command -v" in text and "Xvfb" in text, "Dockerfile must assert `Xvfb` is on PATH (mirroring the existing `command -v` liveness checks)."


def test_dockerfile_installs_patchright_real_chrome() -> None:
    """Guard (b): Patchright's real Chrome channel must be installed, not just the pip package.

    `channel="chrome"` (tracklist_render_browser_channel default) is Patchright's most convincing
    configuration -- more so than its bundled patched Chromium -- so the image must install an
    actual Google Chrome via `patchright install ... chrome`.
    """
    text = _dockerfile_text()
    assert "patchright install" in text, "Dockerfile must run `patchright install ... chrome` to provision the real Chrome browser (phaze-fq9h.5)."
    assert "chrome" in text.lower(), (
        "Dockerfile's patchright install must target the `chrome` channel (matches tracklist_render_browser_channel default)."
    )


def test_dockerfile_pins_playwright_browsers_path() -> None:
    """Guard (c): PLAYWRIGHT_BROWSERS_PATH must be set to a fixed path.

    The Chrome install runs as root (needed for `--with-deps` apt-get), but the browser launches
    later as the unprivileged `phaze` user -- a per-user cache default (`~/.cache/ms-playwright`)
    would leave root's downloaded browser invisible to `phaze` at runtime.
    """
    text = _dockerfile_text()
    assert "PLAYWRIGHT_BROWSERS_PATH" in text, (
        "Dockerfile must set PLAYWRIGHT_BROWSERS_PATH to a fixed path shared between the root install step and the runtime `phaze` user."
    )


def test_chrome_install_precedes_user_switch() -> None:
    """Guard (d): the Chrome install must run BEFORE the USER switch (it needs root for apt-get).

    Matches the USER *directive*, not a particular user spelling: phaze-aip8 changed this
    Dockerfile from `USER phaze` to the numeric `USER 1000:1000` to satisfy hadolint DL3066, and a
    matcher keyed to the name silently stopped finding it. What this guard actually cares about is
    ordering relative to whatever drops privileges, so match that and survive the next rename.
    """
    text = _dockerfile_text()
    lines = text.splitlines()

    install_idx = next((i for i, line in enumerate(lines) if "patchright install" in line), None)
    user_idx = next((i for i, line in enumerate(lines) if line.strip().startswith("USER ")), None)

    assert install_idx is not None, "Dockerfile has no `patchright install` step."
    assert user_idx is not None, "Dockerfile has no USER instruction."
    assert install_idx < user_idx, "`patchright install` must run before the USER switch -- `--with-deps` needs root for its apt-get install."
