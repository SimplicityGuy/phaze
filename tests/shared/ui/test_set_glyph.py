"""phaze-x1qr3.7: render contracts for the set glyph and its legend.

Two Jinja macros in ``ui/primitives.html``: ``set_glyph`` (one inline-SVG ``<rect>`` per
COARSE-window cell of ``set_profile.glyph``) and ``set_glyph_legend`` (twelve Camelot hue
swatches, a four-step "quiet -> peak" lightness scale, and the segment count). Both call the
SAME Python functions -- ``camelot_hue`` / ``energy_lightness`` in
``services/set_glyph_colors.py`` -- registered here as Jinja globals exactly as
``routers/record.py`` registers them for the live page (see that module's
``templates.env.globals`` block), so this test exercises the real formula, not a re-derived
copy of it.

``set_profile.glyph`` is ``None`` for "no coarse windows" (a fine-only file, or one never
projected) -- never an empty list -- per ``services/set_projection._glyph_cells``. Cells are
plain dicts (``{"camelot_number": int | None, "energy": float | None}``), matching
``SetProfileProjection.glyph``'s stored shape; a ``SimpleNamespace`` stands in for the
``SetProfile`` ORM row since only attribute access (``profile.glyph``) is used.
"""

from __future__ import annotations

from pathlib import Path
import re
from types import SimpleNamespace

from fastapi.templating import Jinja2Templates

from phaze.services.set_glyph_colors import CAMELOT_LEGEND, ENERGY_LIGHTNESS_STEP_COUNT, camelot_hue, energy_lightness


TEMPLATES_DIR = Path(__file__).resolve().parents[3] / "src" / "phaze" / "templates"
_templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# Mirrors routers/record.py's registration exactly -- the same functions, the same names --
# so a divergence between the live page's globals and this test's would show up as a name the
# macro cannot resolve, not as a silently different formula.
_templates.env.globals["camelot_hue"] = camelot_hue
_templates.env.globals["energy_lightness"] = energy_lightness
_templates.env.globals["camelot_legend"] = CAMELOT_LEGEND
_templates.env.globals["energy_lightness_step_count"] = ENERGY_LIGHTNESS_STEP_COUNT

_GLYPH_IMPORT = '{% import "ui/primitives.html" as ui %}'
_RECT = re.compile(r'<rect\b[^>]*\bfill="hsl\((?P<hue>[0-9]+), (?P<sat>[0-9]+)%, (?P<light>[0-9]+)%\)"[^>]*>')
_SWATCH = re.compile(
    r'<li data-legend-swatch data-camelot-number="(?P<number>\d+)"[^>]*>\s*'
    r'<span aria-hidden="true"[^>]*style="background-color: hsl\((?P<hue>[0-9]+), 65%, 45%\)"',
    re.DOTALL,
)


def _render(source: str, **context: object) -> str:
    return _templates.env.from_string(_GLYPH_IMPORT + source).render(**context)


def _cells(*pairs: tuple[int | None, float | None]) -> list[dict[str, int | float | None]]:
    return [{"camelot_number": number, "energy": energy} for number, energy in pairs]


def test_glyph_renders_exactly_one_rect_per_cell() -> None:
    profile = SimpleNamespace(glyph=_cells((8, 0.1), (9, 0.4), (10, 0.9), (11, 0.5), (1, 0.0)))
    html = _render("""{{ ui.set_glyph(profile) }}""", profile=profile)

    rects = _RECT.findall(html)
    assert len(rects) == len(profile.glyph) == 5
    assert html.count("<rect") == 5


def test_glyph_hue_follows_the_camelot_formula() -> None:
    profile = SimpleNamespace(glyph=_cells((1, 0.5), (2, 0.5), (8, 0.5), (12, 0.5)))
    html = _render("""{{ ui.set_glyph(profile) }}""", profile=profile)

    rects = _RECT.finditer(html)
    hues = [int(m.group("hue")) for m in rects]
    expected = [(cell["camelot_number"] - 1) * 30 for cell in profile.glyph]
    assert hues == expected == [0, 30, 210, 330]
    # And it is the SAME function the legend below uses, not a re-derived copy.
    assert hues == [camelot_hue(cell["camelot_number"]) for cell in profile.glyph]


def test_glyph_cell_with_no_resolved_camelot_is_desaturated_not_colorless_at_random() -> None:
    """A cell with no overlapping fine-tier key (``camelot_number`` is ``None``) still renders --
    at 0% saturation, so it never claims a key nothing observed -- rather than being dropped,
    which would break the "exactly N rects for N cells" contract above."""
    profile = SimpleNamespace(glyph=_cells((None, 0.5)))
    html = _render("""{{ ui.set_glyph(profile) }}""", profile=profile)

    rects = list(_RECT.finditer(html))
    assert len(rects) == 1
    assert rects[0].group("sat") == "0"


def test_glyph_lightness_is_monotone_increasing_in_energy() -> None:
    energies = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
    profile = SimpleNamespace(glyph=_cells(*((8, e) for e in energies)))
    html = _render("""{{ ui.set_glyph(profile) }}""", profile=profile)

    lightnesses = [int(m.group("light")) for m in _RECT.finditer(html)]
    assert lightnesses == sorted(lightnesses)
    assert len(set(lightnesses)) == len(lightnesses), "distinct energies must render distinct lightness"
    # And, again, it is the shared function -- not an inline re-derivation in the template.
    assert lightnesses == [energy_lightness(e) for e in energies]


def test_glyph_cell_with_no_energy_renders_the_midpoint_lightness() -> None:
    profile = SimpleNamespace(glyph=_cells((8, None)))
    html = _render("""{{ ui.set_glyph(profile) }}""", profile=profile)

    (rect,) = list(_RECT.finditer(html))
    assert int(rect.group("light")) == energy_lightness(None)


def test_fine_only_file_renders_no_coarse_windows_text_not_a_strip() -> None:
    """``glyph`` is ``None`` -- never ``[]`` -- for a fine-only or never-projected file
    (``services/set_projection._glyph_cells``); this is the whole point of that contract."""
    fine_only = SimpleNamespace(glyph=None)
    html = _render("""{{ ui.set_glyph(profile) }}""", profile=fine_only)

    assert "<rect" not in html and "<svg" not in html
    assert "No coarse windows" in html
    assert "data-set-glyph-empty" in html


def test_no_profile_at_all_also_renders_no_coarse_windows_text() -> None:
    """A file never analyzed to completion has no ``SetProfile`` row at all -- ``None``, not an
    empty stand-in -- and must degrade exactly like the fine-only case."""
    html = _render("""{{ ui.set_glyph(profile) }}""", profile=None)

    assert "<rect" not in html and "<svg" not in html
    assert "No coarse windows" in html


def test_glyph_carries_an_aria_label_naming_both_encodings() -> None:
    profile = SimpleNamespace(glyph=_cells((8, 0.5), (9, 0.7)))
    html = _render("""{{ ui.set_glyph(profile) }}""", profile=profile)

    match = re.search(r'<svg\b[^>]*\baria-label="([^"]+)"', html)
    assert match, "the glyph <svg> has no aria-label"
    label = match.group(1).lower()
    assert 'role="img"' in html.lower()
    assert "camelot" in label or "key" in label
    assert "energy" in label
    assert "2" in match.group(1)  # names the segment count too


def test_glyph_carries_a_cursor_marker_hook_for_the_inspection_bead() -> None:
    """An inert, hidden hook -- bead .10's pointer/keyboard inspection positions and updates it;
    this macro's only job is to guarantee the element exists, in BOTH branches (data and empty).

    ``phaze-x1qr3.10`` gave it a class and put it inside a ``relative`` frame, because an
    absolutely-positioned marker needs a positioned ancestor to be positioned INSIDE -- without
    one it would be placed against whatever positioned box happened to be up the tree, which on
    the record page is the article and on a Files row is the table. The frame wraps BOTH
    branches, so the marker's containing block does not depend on whether the file had coarse
    windows.
    """
    with_data = _render("""{{ ui.set_glyph(profile) }}""", profile=SimpleNamespace(glyph=_cells((8, 0.5))))
    without_data = _render("""{{ ui.set_glyph(profile) }}""", profile=SimpleNamespace(glyph=None))

    for html in (with_data, without_data):
        assert '<span data-set-glyph-cursor class="set-glyph-cursor" hidden aria-hidden="true"></span>' in html
        assert '<span data-set-glyph-frame class="relative block">' in html
        assert html.rstrip().endswith("</span>")


def test_legend_has_exactly_twelve_swatches_labelled_with_camelot_number_and_minor_key() -> None:
    html = _render("""{{ ui.set_glyph_legend(4) }}""")

    swatches = list(_SWATCH.finditer(html))
    assert len(swatches) == 12
    numbers = [int(m.group("number")) for m in swatches]
    assert numbers == list(range(1, 13))
    for number, key_name in CAMELOT_LEGEND:
        assert f"{number}A · {key_name}" in html


def test_legend_swatches_use_the_same_hue_formula_as_the_glyph() -> None:
    html = _render("""{{ ui.set_glyph_legend(0) }}""")

    for match in _SWATCH.finditer(html):
        number = int(match.group("number"))
        assert int(match.group("hue")) == camelot_hue(number) == (number - 1) * 30


def test_legend_lightness_scale_has_four_steps_from_quiet_to_peak() -> None:
    html = _render("""{{ ui.set_glyph_legend(0) }}""")

    steps = re.findall(r'<li data-legend-lightness-step data-step-index="(\d)"', html)
    assert len(steps) == ENERGY_LIGHTNESS_STEP_COUNT == 4
    assert steps == [str(i) for i in range(4)]
    assert "Quiet" in html and "Peak" in html
    lightness_values = [int(m) for m in re.findall(r"hsl\(0, 0%, (\d+)%\)", html)]
    assert lightness_values == sorted(lightness_values)
    assert lightness_values[0] == energy_lightness(0.0)
    assert lightness_values[-1] == energy_lightness(1.0)


def test_legend_reports_the_glyph_cell_count() -> None:
    zero = _render("""{{ ui.set_glyph_legend(0) }}""")
    one = _render("""{{ ui.set_glyph_legend(1) }}""")
    many = _render("""{{ ui.set_glyph_legend(37) }}""")

    assert "0 segments" in zero
    assert "1 segment" in one and "1 segments" not in one
    assert "37 segments" in many


def test_glyph_height_class_defaults_to_h7_and_is_overridable() -> None:
    """phaze-x1qr3.9: row-scale callers (Files table, Changes Review, ⌘K palette) pass
    ``height_class='h-2.5'`` (10px) instead of the record page's full ``h-7`` -- ONE rendering
    path, only the Tailwind height class varies. The record page's existing two-arg call
    (``ui.set_glyph(profile, id)``) is unaffected by the new keyword-only-in-practice default."""
    profile = SimpleNamespace(glyph=_cells((8, 0.5)))
    default_html = _render("""{{ ui.set_glyph(profile) }}""", profile=profile)
    small_html = _render("""{{ ui.set_glyph(profile, height_class='h-2.5') }}""", profile=profile)

    assert 'class="block h-7 w-full' in default_html
    assert 'class="block h-2.5 w-full' in small_html


def test_glyph_and_legend_content_is_autoescaped() -> None:
    """The macros interpolate no operator-controlled string content (unlike, say, ``page_header``'s
    ``title``), but a caller passing a hostile ``id`` must not break out of the attribute."""
    html = _render("""{{ ui.set_glyph(profile, id) }}""", profile=SimpleNamespace(glyph=None), id='"><script>alert(1)</script>')

    assert "<script>alert(1)</script>" not in html
