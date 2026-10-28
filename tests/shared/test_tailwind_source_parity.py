"""Guard: the image and the dev/CI browser suite compile the SAME app.css (phaze-oicyy).

Two producers build ``src/phaze/static/css/app.css``: the Dockerfile ``css-builder`` stage (which COPYs
only a few paths into an empty ``/build``) and ``just build-tailwind`` (run from the repo root, which backs
``just test-browser`` and the blocking Browser-contract CI job). Tailwind v4 auto-detects class
candidates from the cwd as well as from explicit ``@source`` lines, so the two used to scan different
trees: 1,266 vs 899 class selectors, and ``dark:ring-phaze-border`` (emitted only by Python in
``routers/pipeline/skip.py``) existed in the tested CSS but not in the shipped one.

The fix is ``source(none)`` plus an explicit ``@source`` list. These tests pin it three ways:

* static: ``source(none)`` is set, every Python module emitting ``class="..."`` markup is a ``@source``,
  and every ``@source`` path is COPYed by the css-builder stage;
* real consumer (``browser`` marker, run by ``just test-browser`` where the pinned binary exists): build
  with the real Tailwind binary from the repo root and from a tree holding only what css-builder COPYs,
  and require byte-identical CSS that contains the toast's dark ring.
"""

from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess

import pytest

from tests._child_process_budget import CHILD_PROCESS_HANG_GUARD_SEC


_ROOT = Path(__file__).resolve().parents[2]
_APP_CSS = _ROOT / "assets" / "src" / "app.css"
_DOCKERFILE = _ROOT / "Dockerfile"
_TAILWIND = _ROOT / "bin" / "tailwindcss"
_EMITTER = re.compile(r"""(?<![A-Za-z0-9_])class=\\?["']""")


def _sources() -> list[str]:
    """Repo-relative paths named by the ``@source`` lines of the input stylesheet."""
    css = _APP_CSS.read_text(encoding="utf-8")
    found = re.findall(r'^@source\s+"([^"]+)";', css, re.MULTILINE)
    return [(_APP_CSS.parent / rel).resolve().relative_to(_ROOT).as_posix() for rel in found]


def _css_builder_copies() -> list[tuple[list[str], str]]:
    """``(sources, dest)`` for every COPY in the css-builder stage of the Dockerfile."""
    stage = _DOCKERFILE.read_text(encoding="utf-8").split("AS css-builder", 1)[1].split("\nFROM ", 1)[0]
    copies = []
    for line in stage.splitlines():
        if line.startswith("COPY ") and "--from" not in line:
            *srcs, dest = line.split()[1:]
            copies.append((srcs, dest))
    return copies


def _build_image_tree(dest_root: Path) -> None:
    """Reproduce the css-builder stage's filesystem: only what its COPY lines bring in."""
    for srcs, dest in _css_builder_copies():
        target = dest_root / dest
        for src in srcs:
            origin = _ROOT / src
            if origin.is_dir():
                shutil.copytree(origin, target, dirs_exist_ok=True)
            else:
                target.mkdir(parents=True, exist_ok=True)
                shutil.copy2(origin, target / origin.name)


def _compile(cwd: Path, out: Path) -> str:
    subprocess.run(  # noqa: S603 -- pinned local binary, fixed argv
        [str(_TAILWIND), "-i", "assets/src/app.css", "-o", str(out), "--minify"],
        cwd=cwd,
        check=True,
        capture_output=True,
        timeout=CHILD_PROCESS_HANG_GUARD_SEC,
    )
    return out.read_text(encoding="utf-8")


def test_automatic_source_detection_is_off() -> None:
    assert re.search(r'^@import\s+"tailwindcss"\s+source\(none\);', _APP_CSS.read_text(encoding="utf-8"), re.MULTILINE), (
        'assets/src/app.css must `@import "tailwindcss" source(none)`: automatic detection scans the cwd, so the repo-root '
        "build and the Dockerfile build would again produce different CSS."
    )


def test_every_python_class_emitter_is_a_tailwind_source() -> None:
    emitters = {p.relative_to(_ROOT).as_posix() for p in (_ROOT / "src" / "phaze").rglob("*.py") if _EMITTER.search(p.read_text(encoding="utf-8"))}
    missing = emitters - set(_sources())
    assert not missing, (
        f'Python modules emitting class="..." markup but absent from app.css @source (their utilities would not ship): {sorted(missing)}'
    )


def test_every_tailwind_source_is_copied_by_the_css_builder_stage() -> None:
    copied: set[str] = set()
    for srcs, dest in _css_builder_copies():
        for src in srcs:
            if (_ROOT / src).is_dir():
                copied.add(src.rstrip("/") + "/")
            else:
                copied.add((Path(dest) / Path(src).name).as_posix())
    for source in _sources():
        assert any(source == c.rstrip("/") or source.startswith(c) for c in copied), (
            f"app.css @source {source} is not COPYed by the Dockerfile css-builder stage, so the image would compile without it"
        )


@pytest.mark.browser
def test_repo_root_and_image_builds_produce_identical_css(tmp_path: Path) -> None:
    assert _TAILWIND.is_file(), "bin/tailwindcss missing: run `just build-tailwind` (test-browser depends on it)"
    image_tree = tmp_path / "image"
    _build_image_tree(image_tree)
    image_css = _compile(image_tree, tmp_path / "image.css")
    root_css = _compile(_ROOT, tmp_path / "root.css")
    assert image_css == root_css, "the repo-root build (browser CI) and the css-builder-equivalent build (image) diverge"
    # The one class the old divergence dropped from production: emitted only by skip.py's toasts.
    assert r"dark\:ring-phaze-border" in image_css
