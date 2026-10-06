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
from phaze.utils.humanize import format_count, format_duration


if TYPE_CHECKING:
    from jinja2 import Environment


# ---------------------------------------------------------------------------------------------------
# Page names (phaze-yyfax) -- the ONE source of truth for each shell page's display name.
# The ONE source of truth for each shell page's display name (phaze-yyfax).
#
# A page's name used to be spelled five separate times -- the rail label, the workspace ``<h1>``, the
# document title (``DOCUMENT_TITLES``), the Summary card / alert-link text and the command palette --
# and the copies drifted: the rail said "Tracklists", the ``<h1>`` said "TRACKLIST"; the rail said
# "Duplicates", the ``<h1>`` said "REVIEW & APPLY · DEDUPE"; the palette said "Overview" for the page
# the rail calls "Summary". Every one of those surfaces now reads from :data:`PAGE_NAMES`.
#
# Where the old copies disagreed, the RAIL label won (it is the name the operator navigates by), with
# one deliberate exception: the last stage is named **Execute**, not "Execute approved". The Files
# matrix column and the record page's stage list already said "Execute", the backing route is
# ``/execution/start`` and its log is the execution log, and "Execute approved" is a verb phrase that
# cannot head a matrix column. The Summary card's "Apply" was the odd one out and is retired.
#
# Pure data, no Jinja and no I/O: the rail / workspace templates reach it through the globals
# :func:`phaze.web.template_globals.register_page_name_globals` puts on every ``Jinja2Templates``
# environment, and Python callers (the Summary router, the tests) import it directly.
# ---------------------------------------------------------------------------------------------------

# Stage key -> display name. Keys are the ``/s/<stage>`` path segments. ``tagwrite`` / ``move`` are
# compatibility aliases that render the Changes Review workspace, so they share its name.
PAGE_NAMES: dict[str, str] = {
    "summary": "Summary",
    "files": "Files",
    "discover": "Discover",
    "metadata": "Metadata",
    "analyze": "Analyze",
    "tracklist": "Tracklists",
    "propose": "Propose changes",
    "rename": "Changes Review",
    "tagwrite": "Changes Review",
    "move": "Changes Review",
    "dedupe": "Duplicates",
    "cue": "Cue sheets",
    "apply": "Execute",
    "audit": "Audit log",
    "agents": "Agents & compute lanes",
    "runtime-config": "Config",
}

# Pages the ⌘K palette lists, in rail order, with Config (opened from the header gear, not the rail)
# last. Aliases are excluded: they are not navigation destinations of their own.
PALETTE_PAGE_KEYS: tuple[str, ...] = (
    "summary",
    "files",
    "discover",
    "metadata",
    "analyze",
    "tracklist",
    "propose",
    "rename",
    "dedupe",
    "cue",
    "apply",
    "audit",
    "agents",
    "runtime-config",
)


def page_name(stage: str) -> str:
    """The display name of ``stage``; ``KeyError`` for an unknown stage (never a silent fallback)."""
    return PAGE_NAMES[stage]


def open_label(stage: str) -> str:
    """The label of a link that opens ``stage``: always ``Open <page name>``, so it names a real page."""
    return f"Open {PAGE_NAMES[stage]}"


def palette_pages() -> list[tuple[str, str, str]]:
    """``(stage, name, href)`` for every palette-navigable page, in display order."""
    return [(key, PAGE_NAMES[key], f"/s/{key}") for key in PALETTE_PAGE_KEYS]


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
    # ``ui/primitives.html`` (which hosts ``set_glyph_legend``) uses the display-format filters at COMPILE time, so any
    # environment that can import it needs them; registering here keeps the two helpers from being a trap in either order.
    register_format_filters(env)


def register_page_name_globals(env: Environment) -> None:
    """Register ``page_name(stage)`` and ``palette_pages()`` on ``env`` (phaze-yyfax).

    Every template that prints a page's name (rail, workspace ``<h1>``, palette) calls these instead
    of spelling the string, so the name has one definition (``phaze.web.template_globals``).
    """
    env.globals["page_name"] = page_name
    env.globals["palette_pages"] = palette_pages
    register_format_filters(env)


def register_format_filters(env: Environment) -> None:
    """Register the display-format Jinja filters on ``env`` (phaze-nwmsu).

    Called from :func:`register_page_name_globals`, which every ``Jinja2Templates`` environment
    already calls, so the filters reach all of them without a second registration line per router.
    ``duration`` renders seconds as ``h:mm:ss`` / ``m:ss`` / ``—``; ``thousands`` renders an integer count with ``,``
    separators (phaze-dwevc). The name is not ``count``: Jinja already ships ``count`` as an alias of ``length``.
    """
    env.filters["duration"] = format_duration
    env.filters["thousands"] = format_count


__all__ = [
    "PAGE_NAMES",
    "PALETTE_PAGE_KEYS",
    "open_label",
    "page_name",
    "palette_pages",
    "register_format_filters",
    "register_page_name_globals",
    "register_set_glyph_globals",
]
