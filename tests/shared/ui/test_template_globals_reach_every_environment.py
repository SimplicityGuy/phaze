"""Every ``Jinja2Templates`` environment in the app carries the four set-glyph globals.

WHAT THIS CATCHES. ``Jinja2Templates(...)`` builds a fresh ``jinja2.Environment`` per call and
``Environment.globals`` is per-instance, so a global registered by one router is invisible to
every other. The four set-glyph globals were hand-copied into five of the thirteen environments
and absent from the other eight (PR #556 review, finding 1); a partial calling ``camelot_hue``
therefore rendered on the record page and raised ``jinja2.UndefinedError`` at REQUEST time on
any surface whose router had never copied the block. Nothing at import time noticed, and no
unit test did either, because each test rendered through an environment it built itself.

WHY IT ENUMERATES THE TREE RATHER THAN A LIST. A hand-maintained list of routers is exactly the
artefact that went stale here. This walks ``src/phaze`` for every module that constructs an
environment, imports it, and reads the environment the module actually built -- so a
FOURTEENTH router added tomorrow without the registration call fails this test on the day it
lands, with no list to remember to update.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest


SRC_ROOT = Path(__file__).resolve().parents[3] / "src"
PACKAGE_ROOT = SRC_ROOT / "phaze"

# The names ``ui/primitives.html``'s ``set_glyph`` / ``set_glyph_legend`` macros and the harmonic
# wheel resolve out of ``Environment.globals``. Spelled out here rather than imported from the
# registration helper so a name silently dropped THERE fails HERE rather than agreeing with it.
REQUIRED_GLOBALS = ("camelot_hue", "camelot_legend", "energy_lightness", "energy_lightness_step_count")


def _modules_building_a_template_environment() -> list[str]:
    """Dotted module names for every ``src/phaze`` module with a module-level ``templates = Jinja2Templates(...)``.

    Parsed with ``ast`` rather than matched with a regex: the thing that must be found is a
    module-level ASSIGNMENT whose value is that call, and a commented-out line or a mention in a
    docstring is neither.
    """
    found: list[str] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            call = node.value
            if "templates" not in targets or not isinstance(call, ast.Call):
                continue
            callee = call.func
            name = callee.attr if isinstance(callee, ast.Attribute) else callee.id if isinstance(callee, ast.Name) else ""
            if name == "Jinja2Templates":
                found.append(".".join(path.relative_to(SRC_ROOT).with_suffix("").parts))
    return found


def test_the_tree_still_has_the_thirteen_environments_this_guard_is_about() -> None:
    """A floor on the discovery itself: a guard that found NOTHING would pass silently."""
    modules = _modules_building_a_template_environment()

    assert len(modules) >= 13, f"expected at least the thirteen known template environments, found {modules}"
    assert "phaze.routers.record" in modules
    assert "phaze.routers.duplicates" in modules, "one of the eight that had NO set-glyph globals before this bead"


@pytest.mark.parametrize("module_name", _modules_building_a_template_environment())
def test_every_template_environment_carries_the_set_glyph_globals(module_name: str) -> None:
    """Read off the environment the module actually built, not off its source text.

    Asserting on the imported object is what makes this a claim about what a render will
    resolve: a registration call present in the file but shadowed, reassigned, or made against a
    different environment would still fail here.
    """
    environment = importlib.import_module(module_name).templates.env

    missing = [name for name in REQUIRED_GLOBALS if name not in environment.globals]
    assert not missing, f"{module_name} renders with an environment missing {missing}"


@pytest.mark.parametrize("module_name", _modules_building_a_template_environment())
def test_every_environment_binds_the_SAME_formula_objects(module_name: str) -> None:
    """Identity, not merely presence -- the failure the copies made possible was two surfaces
    computing a Camelot hue from two different functions, which every "is the name defined"
    check in the world passes."""
    from phaze.services.set_glyph_colors import CAMELOT_LEGEND, ENERGY_LIGHTNESS_STEP_COUNT, camelot_hue, energy_lightness

    globals_ = importlib.import_module(module_name).templates.env.globals

    assert globals_["camelot_hue"] is camelot_hue
    assert globals_["energy_lightness"] is energy_lightness
    assert globals_["camelot_legend"] is CAMELOT_LEGEND
    assert globals_["energy_lightness_step_count"] == ENERGY_LIGHTNESS_STEP_COUNT
