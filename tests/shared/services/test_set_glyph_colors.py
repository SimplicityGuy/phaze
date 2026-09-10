"""Pure-function unit tests for the shared Camelot-hue / energy-lightness formulas.

Companion to ``tests/shared/ui/test_set_glyph.py``, which exercises these same functions
THROUGH the Jinja macros (the render contract phaze-x1qr3.7's acceptance names). These tests
exercise the functions directly, including the out-of-range branches a rendered glyph never
reaches because every stored ``camelot_number`` is either ``None`` or one of ``CAMELOT_TABLE``'s
1-12 -- ``camelot_hue`` and ``energy_lightness`` are still public, never-raise functions, and
future callers (bead phaze-x1qr3.8's harmonic-journey wheel) may not enjoy that same guarantee.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from phaze.services import set_glyph_colors as set_glyph_colors_module
from phaze.services.set_glyph_colors import (
    CAMELOT_HUE_STEP,
    CAMELOT_LEGEND,
    ENERGY_LIGHTNESS_MAX,
    ENERGY_LIGHTNESS_MIN,
    ENERGY_LIGHTNESS_STEP_COUNT,
    camelot_hue,
    energy_lightness,
)
from phaze.services.set_projection import CAMELOT_TABLE, key_name_for_camelot


@pytest.mark.parametrize("number", range(1, 13))
def test_camelot_hue_matches_the_formula_for_every_wheel_position(number: int) -> None:
    assert camelot_hue(number) == (number - 1) * CAMELOT_HUE_STEP == (number - 1) * 30


@pytest.mark.parametrize("bad", [None, 0, 13, -1, 100])
def test_camelot_hue_returns_none_never_raises_outside_the_wheel(bad: int | None) -> None:
    assert camelot_hue(bad) is None


def test_camelot_hue_accepts_a_float_that_is_really_an_integer() -> None:
    """``AnalysisWindow``/glyph cells never store a float camelot_number, but the signature
    accepts one rather than asserting a type nothing upstream can violate."""
    assert camelot_hue(8.0) == camelot_hue(8)


@pytest.mark.parametrize("energy", [0.0, 0.25, 0.5, 0.75, 1.0])
def test_energy_lightness_is_linear_and_bounded(energy: float) -> None:
    value = energy_lightness(energy)
    assert ENERGY_LIGHTNESS_MIN <= value <= ENERGY_LIGHTNESS_MAX
    assert value == round(ENERGY_LIGHTNESS_MIN + energy * (ENERGY_LIGHTNESS_MAX - ENERGY_LIGHTNESS_MIN))


def test_energy_lightness_clamps_out_of_range_input() -> None:
    assert energy_lightness(-1.0) == energy_lightness(0.0) == ENERGY_LIGHTNESS_MIN
    assert energy_lightness(2.0) == energy_lightness(1.0) == ENERGY_LIGHTNESS_MAX


def test_energy_lightness_of_none_is_the_scale_midpoint() -> None:
    midpoint = energy_lightness(None)
    assert midpoint == (ENERGY_LIGHTNESS_MIN + ENERGY_LIGHTNESS_MAX) // 2
    assert ENERGY_LIGHTNESS_MIN < midpoint < ENERGY_LIGHTNESS_MAX


def test_energy_lightness_is_strictly_monotone_over_distinct_energies() -> None:
    energies = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
    values = [energy_lightness(e) for e in energies]
    assert values == sorted(values)
    assert len(set(values)) == len(values)


def test_camelot_legend_has_twelve_entries_in_wheel_order_matching_camelot_table() -> None:
    assert len(CAMELOT_LEGEND) == 12
    assert [number for number, _ in CAMELOT_LEGEND] == list(range(1, 13))
    code_to_key = {code: key for key, code in CAMELOT_TABLE.items()}
    for number, key_name in CAMELOT_LEGEND:
        assert code_to_key[f"{number}A"] == key_name
        assert key_name.endswith(" minor")


def test_energy_lightness_step_count_is_four() -> None:
    assert ENERGY_LIGHTNESS_STEP_COUNT == 4


def test_the_legend_reads_the_projections_own_inverse_rather_than_inverting_the_table_again() -> None:
    """``CAMELOT_LEGEND`` is built by CALLING ``set_projection.key_name_for_camelot``.

    The values it produces are checked above against an independently-inverted table, which is
    the right check for CONTENT and cannot see the thing this bead is about: a second inversion
    living here is equivalent on the day it is written and silently keeps the old answer the day
    the shared inverse changes (an enharmonic spelling, a table entry), while the legend and the
    swatches it labels still agree with each other. So this asserts the DEPENDENCY -- the
    function is the one called, and no local inversion of ``CAMELOT_TABLE`` remains to drift.
    """
    module_source = Path(set_glyph_colors_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(module_source)
    builder = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_minor_key_names")
    called = {node.func.id for node in ast.walk(builder) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) for alias in node.names}

    assert "key_name_for_camelot" in called, "the legend must read the projection's own inverse"
    assert "CAMELOT_TABLE" not in imported, "importing the table back here is how a second inversion returns"
    assert [(number, key_name_for_camelot(f"{number}A")) for number in range(1, 13)] == list(CAMELOT_LEGEND)
