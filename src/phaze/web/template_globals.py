"""The Jinja globals every ``Jinja2Templates`` environment in this app must carry.

WHY THIS EXISTS. FastAPI's ``Jinja2Templates(...)`` builds a FRESH ``jinja2.Environment`` per
call, and this app constructs thirteen of them -- one per router module that renders HTML.
``env.globals`` is per-environment, so a global registered in ``routers/record.py`` is invisible
to ``routers/duplicates.py``. The set-glyph colour globals were therefore hand-copied into five
of the thirteen and simply absent from the other eight; a partial that calls ``camelot_hue``
renders fine on the record page and raises ``UndefinedError`` at REQUEST time on any surface
whose router never happened to copy the block (PR #556 review, finding 1).

One registration function, called from every environment, makes "which router am I rendered
from" stop being a fact a partial has to know. It also removes the reason a new surface would
have to re-copy anything: adding a global here reaches all thirteen at once.

Lives under ``phaze/web/`` rather than beside the formulas in
``services/set_glyph_colors.py`` on purpose -- that module's docstring commits to "no I/O, no
templates, no models imported at call time", and importing Jinja into it to register Jinja
globals would be exactly the coupling it declines. The formulas stay pure; the wiring lives in
the web layer that owns the environments.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from phaze.services.set_glyph_colors import CAMELOT_LEGEND, ENERGY_LIGHTNESS_STEP_COUNT, camelot_hue, energy_lightness


if TYPE_CHECKING:
    from jinja2 import Environment


def register_set_glyph_globals(env: Environment) -> None:
    """Register the four set-glyph globals on ``env``.

    The two FORMULAS (``camelot_hue``, ``energy_lightness``) are registered as the Python
    functions themselves rather than as pre-computed values, so ``ui/primitives.html``'s
    ``set_glyph`` macro, its legend, the harmonic wheel's sector tints and the poster all call
    ONE implementation and cannot drift on what Camelot position "8A" renders as. The two
    CONSTANTS (the legend's twelve key names, the lightness scale's step count) come from the
    same module, so the legend a surface draws and the colours it draws with are derived from a
    single table.

    Idempotent: calling it twice on one environment rebinds the same four names to the same four
    objects. Registers nothing else -- ``static_url`` and ``humanize_relative_time`` are wanted
    by only some environments and stay at their own call sites.
    """
    env.globals["camelot_hue"] = camelot_hue
    env.globals["energy_lightness"] = energy_lightness
    env.globals["camelot_legend"] = CAMELOT_LEGEND
    env.globals["energy_lightness_step_count"] = ENERGY_LIGHTNESS_STEP_COUNT


__all__ = ["register_set_glyph_globals"]
