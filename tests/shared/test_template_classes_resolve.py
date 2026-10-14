"""Guard: every static class token in a template must resolve to a selector in the built CSS (phaze-zrkcb).

Tailwind emits nothing for an unknown utility, so a misspelt class, or one naming a theme token that does not
exist (``dark:ring-phaze-border-lg`` shipped in 8 templates for months, with no ``--color-phaze-border-lg``
anywhere), renders unstyled with no error. Nothing else in the repo compares a template class to a CSS rule.

THE CSS IS BUILT THE WAY THE IMAGE BUILDS IT, NOT READ FROM ``src/phaze/static/css/app.css``. Tailwind v4
auto-detects sources from the working directory in addition to ``@source``, so a build run from the repo root
scans tests, docs and Python and emits a SUPERSET (measured 2026-10-14: 1,266 selectors against the image's 899).
A guard reading that file would pass for a class that exists only because a test string mentions it. The
Dockerfile ``css-builder`` stage copies exactly ``assets/`` and ``src/phaze/templates/`` into an empty
``/build`` and runs ``tailwindcss -i assets/src/app.css -o src/phaze/static/css/app.css --minify`` there; this
test does the same in a temporary directory.

Only literal `class="..."` attributes are checked (the bead's "static" tokens); Alpine `:class` bindings are not.

Needs the pinned standalone binary (``just tailwind`` leaves it at ``./bin/tailwindcss``). Absent, the test skips
loudly, unless ``PHAZE_REQUIRE_TAILWIND=1``, which makes absence a failure.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest


_ROOT = Path(__file__).resolve().parents[2]
_PINNED = re.search(r'^tailwind_version := "([^"]+)"', (_ROOT / "justfile").read_text(encoding="utf-8"), re.MULTILINE)

# Class tokens that are deliberately NOT Tailwind utilities: state classes toggled by JS or by the
# server, and hooks that scripts and tests select on. They style nothing by themselves, so a missing
# selector is correct. Adding to this set is a decision: say what reads the token.
_NOT_STYLING = {
    "open": "state class toggled on the drawer/disclosure elements by script; styled through the attribute or ancestor selectors",
    "selected": "state class set on the active row by script; styled through the attribute selectors",
    "rail-node": "JS and test selector hook for the pipeline rail nodes; carries no styling of its own",
}

_CLASS_ATTR = re.compile(r"""(?<![\w:.@-])class\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_JINJA_EXPR = re.compile(r"\{\{.*?\}\}", re.DOTALL)
_JINJA_TAG = re.compile(r"\{%.*?%\}|\{#.*?#\}", re.DOTALL)
_CSS_CLASS = re.compile(r"\.((?:\\[0-9a-fA-F]{1,6} ?|\\.|[\w-])+)")
_CSS_ESCAPE = re.compile(r"\\(?:([0-9a-fA-F]{1,6}) ?|(.))")


def _find_tailwind() -> Path | None:
    candidates = [_ROOT / "bin" / "tailwindcss"]
    common = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],  # noqa: S607
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if common:
        # A bh worktree has no bin/ of its own; the main clone's gitignored copy is the same pinned binary.
        candidates.append((_ROOT / common).resolve().parent / "bin" / "tailwindcss")
    found = shutil.which("tailwindcss")
    if found:
        candidates.append(Path(found))
    return next((c for c in candidates if c.is_file() and os.access(c, os.X_OK)), None)


def static_class_tokens(html: str) -> set[str]:
    """Class tokens a template writes literally. A token touching a ``{{ }}`` expression is dynamic and dropped."""
    tokens: set[str] = set()
    for match in _CLASS_ATTR.finditer(html):
        value = match.group(1) if match.group(1) is not None else match.group(2)
        value = _JINJA_EXPR.sub("\x00", _JINJA_TAG.sub(" ", value))
        tokens.update(t for t in value.split() if "\x00" not in t)
    return tokens


def css_class_names(css: str) -> set[str]:
    """Every class name appearing in a selector of ``css``, unescaped (``md\\:flex`` -> ``md:flex``)."""

    def unescape(m: re.Match[str]) -> str:
        return chr(int(m.group(1), 16)) if m.group(1) else m.group(2)

    return {_CSS_ESCAPE.sub(unescape, raw) for raw in _CSS_CLASS.findall(css)}


def dead_class_tokens(templates: Path, css: str) -> dict[str, list[str]]:
    """Map each static template class token with no CSS selector (and not allowlisted) to the templates using it."""
    defined = css_class_names(css)
    dead: dict[str, list[str]] = {}
    for path in sorted(templates.rglob("*.html")):
        for token in static_class_tokens(path.read_text(encoding="utf-8")):
            if token not in defined and token not in _NOT_STYLING:
                dead.setdefault(token, []).append(str(path.relative_to(templates)))
    return dead


def build_image_equivalent_css(binary: Path, workdir: Path, templates: Path) -> str:
    """Run Tailwind as the Dockerfile ``css-builder`` stage does: cwd holds ONLY ``assets/`` and the templates."""
    shutil.copytree(_ROOT / "assets", workdir / "assets")
    shutil.copytree(templates, workdir / "src" / "phaze" / "templates")
    out = workdir / "src" / "phaze" / "static" / "css" / "app.css"
    subprocess.run(  # noqa: S603
        [str(binary), "-i", "assets/src/app.css", "-o", str(out), "--minify"],
        cwd=workdir,
        check=True,
        capture_output=True,
    )
    return out.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def tailwind() -> Path:
    binary = _find_tailwind()
    if binary is None:
        message = "standalone Tailwind binary not found (run `just tailwind`); the dead-class guard cannot build the image CSS"
        if os.environ.get("PHAZE_REQUIRE_TAILWIND") == "1":
            pytest.fail(message)
        pytest.skip(message)
    assert _PINNED is not None
    version = subprocess.run([str(binary), "--help"], capture_output=True, text=True, check=False).stdout  # noqa: S603
    if _PINNED.group(1).lstrip("v") not in version:
        pytest.skip(f"Tailwind binary is not the pinned {_PINNED.group(1)}; the selector set would differ from the image's")
    return binary


@pytest.fixture(scope="module")
def image_css(tailwind: Path, tmp_path_factory: pytest.TempPathFactory) -> str:
    return build_image_equivalent_css(tailwind, tmp_path_factory.mktemp("image-css"), _ROOT / "src" / "phaze" / "templates")


def test_every_static_template_class_has_a_selector(image_css: str) -> None:
    dead = dead_class_tokens(_ROOT / "src" / "phaze" / "templates", image_css)
    assert not dead, (
        "template classes with no selector in the image-equivalent CSS (a misspelt utility, or one naming a theme token that "
        "does not exist, renders unstyled with no error). Fix the class; allowlist only a state/JS-hook class that styles nothing:\n"
        + "\n".join(f"  {token}  <- {', '.join(files[:3])}{' ...' if len(files) > 3 else ''}" for token, files in sorted(dead.items()))
    )


def test_the_guard_fails_on_a_reintroduced_dead_class(tailwind: Path, tmp_path: Path) -> None:
    """Mutation: the exact defect this bead fixed, planted in a copy of the templates, must be reported."""
    templates = tmp_path / "templates"
    shutil.copytree(_ROOT / "src" / "phaze" / "templates", templates)
    (templates / "zz_mutation.html").write_text('<div class="dark:ring-1 dark:ring-phaze-border-lg input input-sm"></div>', encoding="utf-8")
    css = build_image_equivalent_css(tailwind, tmp_path / "build", templates)
    dead = dead_class_tokens(templates, css)
    assert {"dark:ring-phaze-border-lg", "input", "input-sm"} <= set(dead)
    assert "dark:ring-1" not in dead, "a live variant utility must not be reported"


def test_the_dark_ring_resolves_to_the_design_border_token(image_css: str) -> None:
    assert re.search(r"\.dark\\:ring-phaze-border\b[^{]*\{[^}]*--tw-ring-color:var\(--color-phaze-border\)", image_css), (
        "dark:ring-phaze-border must emit a ring-colour rule bound to the --color-phaze-border design token"
    )


def test_class_token_extraction_and_selector_unescaping() -> None:
    html = """<p class="a md:flex {% if x %}b{% endif %} c-{{ y }} w-1/2" class='d'></p>"""
    assert static_class_tokens(html) == {"a", "md:flex", "b", "w-1/2", "d"}
    assert css_class_names(r".md\:flex{x:y}.w-1\/2:hover{x:y}.\32xl\:p-4{x:y}") == {"md:flex", "w-1/2", "2xl:p-4"}


def test_the_allowlist_names_a_reason_for_every_entry() -> None:
    assert all(reason.strip() for reason in _NOT_STYLING.values())
