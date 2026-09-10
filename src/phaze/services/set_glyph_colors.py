"""Shared Camelot-hue and energy-lightness color formulas for every set-glyph surface.

``phaze-x1qr3.7``. The bead's own requirement is that ONE constant/helper carries the Camelot
hue formula, so the glyph macro (``ui/primitives.html``'s ``set_glyph``), its legend
(``set_glyph_legend``), and -- later, ``phaze-x1qr3.8`` -- the harmonic-journey wheel's sector
tints can never silently disagree on what Camelot position "8A" renders as. Putting it here as
a plain Python function rather than duplicated Jinja arithmetic means every one of those
surfaces, present and future, imports the SAME object; a test that asserts the legend and the
glyph "use the same formula" is really asserting they call the same function.

No I/O, no templates, no models imported at call time -- pure functions over plain values, in
the same spirit as ``services/set_projection.py``, whose ``key_name_for_camelot`` this module
CALLS once at import time to build its legend rather than inverting ``CAMELOT_TABLE`` a second
time of its own.
"""

from __future__ import annotations

from typing import Final

from phaze.services.set_projection import key_name_for_camelot


# Twelve Camelot wheel positions spaced evenly around the hue wheel: 360 / 12 = 30 degrees per
# step, an exact integer for every position from 1 to 12, so no caller ever rounds a hue.
CAMELOT_HUE_STEP: Final[int] = 30


def camelot_hue(camelot_number: int | float | None) -> int | None:
    """Hue in degrees for a Camelot wheel position 1-12, or ``None`` for anything else.

    THE single formula every Camelot-colored surface shares. ``None`` in, ``None`` out --
    never raises -- matching the "gap stays a gap" convention the rest of the projection uses
    (:func:`phaze.services.set_projection.camelot_code`, ``camelot_number``).
    """
    if camelot_number is None:
        return None
    number = int(camelot_number)
    if not (1 <= number <= 12):
        return None
    return (number - 1) * CAMELOT_HUE_STEP


# The "quiet -> peak" lightness scale: a coarse window's energy in [0, 1] maps linearly onto
# this HSL-lightness-percent range. Kept well inside [0, 100] on both ends so neither extreme
# ever renders as pure black or pure white, which would erase the hue entirely.
ENERGY_LIGHTNESS_MIN: Final[int] = 20
ENERGY_LIGHTNESS_MAX: Final[int] = 70
# The legend renders this many swatches across the range (endpoints included), labelled
# "Quiet" -> "Peak" -- the "four-step lightness scale" the bead's acceptance names.
ENERGY_LIGHTNESS_STEP_COUNT: Final[int] = 4


def energy_lightness(energy: float | None) -> int:
    """Lightness percent for an energy value, monotone increasing over ``[0, 1]``.

    ``None`` (no per-window energy stored -- the cross-tier camelot join in
    :func:`phaze.services.set_projection._glyph_cells` can still produce a cell whose energy
    is absent) renders at the scale's midpoint: neither the darkest "quiet" reading nor the
    brightest "peak" one, since neither is true of a gap. Input outside ``[0, 1]`` is clamped
    rather than raising, matching :func:`phaze.services.set_projection._clamp01`'s stance
    elsewhere in the projection.
    """
    if energy is None:
        return (ENERGY_LIGHTNESS_MIN + ENERGY_LIGHTNESS_MAX) // 2
    clamped = max(0.0, min(1.0, energy))
    return round(ENERGY_LIGHTNESS_MIN + clamped * (ENERGY_LIGHTNESS_MAX - ENERGY_LIGHTNESS_MIN))


def _minor_key_names() -> tuple[tuple[int, str], ...]:
    """Camelot number 1-12, in wheel order, paired with its minor-key name.

    Reads :func:`phaze.services.set_projection.key_name_for_camelot` -- the projection's OWN
    inverse of ``CAMELOT_TABLE`` -- rather than inverting that table a second time here. The
    duplicate inversion was equivalent when written and had no way of staying so: a change to
    how a code maps back to a name (an enharmonic spelling, say) would have reached the record
    page's facts list and the tracklist's key column while leaving this legend on the old
    answer, with the legend and the swatches it labels still agreeing with each other.

    Every one of the 12 minor positions ("1A".."12A") is in the table -- the wheel is a
    bijection -- so the lookup never comes back ``None``; the assert states that rather than
    inventing a fallback name for a position that cannot occur.
    """
    names: list[tuple[int, str]] = []
    for number in range(1, 13):
        key = key_name_for_camelot(f"{number}A")
        assert key is not None, f"CAMELOT_TABLE has no minor key at wheel position {number}"
        names.append((number, key))
    return tuple(names)


# The legend's twelve labelled hue swatches: (camelot_number, minor_key_name) in wheel order.
CAMELOT_LEGEND: Final[tuple[tuple[int, str], ...]] = _minor_key_names()


__all__ = [
    "CAMELOT_HUE_STEP",
    "CAMELOT_LEGEND",
    "ENERGY_LIGHTNESS_MAX",
    "ENERGY_LIGHTNESS_MIN",
    "ENERGY_LIGHTNESS_STEP_COUNT",
    "camelot_hue",
    "energy_lightness",
]
