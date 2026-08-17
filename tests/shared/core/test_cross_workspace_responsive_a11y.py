"""phaze-tzy6s.13: responsive + accessibility baseline, enforced across EVERY template at once.

Per-workspace review cannot catch these. Each rule below is a property the whole product must hold,
and each was violated by at least one template that had already passed its own slice's review --
which is the argument for checking them as a set rather than page by page.

Filesystem-only, like ``test_rail_narrow_width.py``: read the templates and assert on the markup. No
DB, no HTTP client, no browser. That keeps the sweep in the fast lane and, more importantly, makes it
exhaustive -- a browser suite can only assert about pages it happens to visit, while this sees all
121 templates including partials that only render in states a smoke test rarely reaches.

Real-browser checks (computed contrast, focus order, axe) are phaze-tzy6s.14's job and do not
duplicate these; the two lanes are complementary, not redundant.
"""

from __future__ import annotations

from pathlib import Path
import re


_TEMPLATES = Path(__file__).resolve().parents[3] / "src" / "phaze" / "templates"

# Jinja comments carry prose about markup ("the no-rows branch renders no <table>"), so they must be
# stripped before any structural scan or the prose registers as markup.
_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)

# A hidden single-purpose carrier table (admin/partials/agents_table.html) that renders no visible
# rows -- it exists to keep a detail row alive while invisible. It has no width and cannot overflow,
# so the overflow-x wrapper rule does not apply. Matched structurally (`<table hidden`), not by path,
# so the exemption cannot silently widen to a real data table in the same file.
_HIDDEN_TABLE = re.compile(r"<table\s+hidden\b")


def _templates() -> list[Path]:
    files = sorted(_TEMPLATES.rglob("*.html"))
    assert len(files) > 100, f"template sweep found only {len(files)} files — is the path right?"
    return files


def _source(path: Path) -> str:
    return _JINJA_COMMENT.sub("", path.read_text())


def test_no_static_viewport_height_units() -> None:
    """Heights use ``dvh``/``svh``, never bare ``vh``.

    On mobile browsers ``100vh`` is the LARGE viewport -- the height the page would have if the URL
    bar were hidden. Sizing to it puts the bottom of the element behind the browser chrome, so the
    last row of a list or the primary button in a sheet is unreachable. ``dvh`` tracks the viewport
    actually visible right now.

    ``vw`` is deliberately NOT covered: viewport width has no equivalent collapsing-chrome problem,
    and the modal ``max-w-[92vw]`` guards are correct as written.
    """
    offenders: list[str] = []
    for path in _templates():
        for match in re.finditer(r"\b(?:max-h|min-h|h)-\[[0-9.]+vh\]", _source(path)):
            offenders.append(f"{path.relative_to(_TEMPLATES)}: {match.group(0)}")
    assert not offenders, "static vh units (use dvh/svh):\n  " + "\n  ".join(offenders)


def test_every_animation_respects_reduced_motion() -> None:
    """Looping animation is gated behind ``motion-safe:``.

    ``prefers-reduced-motion`` is a vestibular-disorder accommodation, not a preference. An ungated
    ``animate-pulse`` keeps moving for a user who has asked the platform for stillness.
    """
    offenders: list[str] = []
    for path in _templates():
        for match in re.finditer(r"(?<!motion-safe:)\banimate-[a-z-]+", _source(path)):
            offenders.append(f"{path.relative_to(_TEMPLATES)}: {match.group(0)}")
    assert not offenders, "ungated animations (prefix with motion-safe:):\n  " + "\n  ".join(offenders)


def test_every_visible_table_scrolls_inside_its_own_container() -> None:
    """A wide table scrolls itself; the PAGE never scrolls sideways.

    The product keeps dense expert tables at every viewport by design. Without an
    ``overflow-x-auto`` ancestor that width propagates to the document, and the whole page --
    header, rail and all -- scrolls horizontally. That is the single most common way a dense admin
    UI becomes unusable on touch, and it is invisible on a desktop review.
    """
    offenders: list[str] = []
    for path in _templates():
        source = _source(path)
        for match in re.finditer(r"<table\b", source):
            if _HIDDEN_TABLE.match(source, match.start()):
                continue
            preceding = source[max(0, match.start() - 500) : match.start()]
            if "overflow-x-auto" not in preceding and "overflow-auto" not in preceding:
                line = source[: match.start()].count("\n") + 1
                offenders.append(f"{path.relative_to(_TEMPLATES)}:{line}")
    assert not offenders, "tables with no overflow-x container (the page will scroll sideways):\n  " + "\n  ".join(offenders)


def test_every_icon_only_control_has_an_accessible_name() -> None:
    """A button whose only content is a glyph carries ``aria-label``.

    Glyphs are ``aria-hidden``, so without a label the control's accessible name is the empty string
    -- it is announced as "button" and is unusable by screen reader. This currently passes across
    every template; the guard exists to keep it that way, since an icon button is the easiest control
    to add without noticing the omission.
    """
    button = re.compile(r"<button\b(?P<attrs>[^>]*)>(?P<inner>.*?)</button>", re.DOTALL)
    offenders: list[str] = []
    for path in _templates():
        source = _source(path)
        for match in button.finditer(source):
            attrs, inner = match.group("attrs"), match.group("inner")
            text = re.sub(r"<svg\b.*?</svg>", "", inner, flags=re.DOTALL)
            text = re.sub(r'<span[^>]*aria-hidden="true"[^>]*>.*?</span>', "", text, flags=re.DOTALL)
            text = re.sub(r"\{%.*?%\}", "", text, flags=re.DOTALL)  # statements only; {{ ... }} IS visible text
            text = re.sub(r"<[^>]+>", "", text).strip()
            if not text and "aria-label" not in attrs:
                line = source[: match.start()].count("\n") + 1
                offenders.append(f"{path.relative_to(_TEMPLATES)}:{line}")
    assert not offenders, "icon-only buttons with no accessible name:\n  " + "\n  ".join(offenders)
