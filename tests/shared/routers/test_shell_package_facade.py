"""Guards on the ``routers/shell`` PACKAGE facade (phaze-bk9el.16).

``src/phaze/routers/shell.py`` was one 1,392-line module -- the busiest file in the repo. It is now
a package, and that is only safe while ``__init__.py`` keeps re-exporting the previous surface off a
single ``router``. The characterization pin (``tests/shared/core/test_shell_characterization.py``)
already covers the served URL space, the stage maps and every rendered binding; this file covers the
two package-shaped failures it structurally cannot see, both of them the ones
``test_pipeline_package_facade`` was written for after the ``routers/pipeline`` split (phaze-oau1o):

1. **``TEMPLATES_DIR`` loses a ``.parent``.** ``stage_maps.py`` sits one directory deeper than the
   old module, so the walk up to ``src/phaze/templates`` needs three hops. ``Jinja2Templates``
   accepts a non-existent directory happily and every render then fails at REQUEST time, far from
   the cause.
2. **A submodule name is shadowed by a re-export.** ``from .summary import summary`` would rebind
   the ``summary`` ATTRIBUTE of the package from the module to the object, after which
   ``monkeypatch.setattr("phaze.routers.shell.summary.get_stage_progress", ...)`` misresolves. Two
   test modules patch exactly that way, because a moved function resolves its globals from the
   module that defines it, not from the package that re-exports it.
"""

from __future__ import annotations

import pkgutil

import phaze.routers.shell as pkg


def test_templates_dir_resolves_to_the_real_template_root() -> None:
    """``TEMPLATES_DIR`` still points at ``src/phaze/templates``, three hops up from the submodule.

    Asserted on the DIRECTORY rather than on a render, because a wrong directory produces a
    perfectly importable module and a 500 at request time -- the failure mode this whole file exists
    to move earlier.
    """
    assert pkg.TEMPLATES_DIR.is_dir(), f"{pkg.TEMPLATES_DIR} does not exist -- a .parent was lost or gained"
    assert (pkg.TEMPLATES_DIR / "shell" / "shell.html").is_file(), f"{pkg.TEMPLATES_DIR} is not the shell's template root"
    assert pkg.templates.env.loader is not None


def test_no_submodule_name_is_shadowed_by_a_re_export() -> None:
    """No submodule shares a name with anything ``__init__`` re-exports, and each stays a module."""
    submodules = {info.name for info in pkgutil.iter_modules(pkg.__path__)}
    assert submodules == {"stage_context", "stage_maps", "summary"}, f"unexpected shell submodule set: {sorted(submodules)}"
    collisions = submodules & set(pkg.__all__)
    assert not collisions, f"submodule names shadowed by a re-export (rename the submodule): {sorted(collisions)}"

    for name in sorted(submodules):
        assert type(getattr(pkg, name)).__name__ == "module", (
            f"phaze.routers.shell.{name} is not the submodule -- patch targets naming it will misresolve"
        )


def test_the_shell_routes_stay_in_the_package_root() -> None:
    """``shell_home`` / ``shell_stage`` are defined in ``phaze.routers.shell`` itself.

    ``test_shell_characterization.test_shell_router_owns_exactly_the_recorded_routes`` filters the
    live app's routes on this exact module string; re-homing either handler to a submodule would
    empty that filter and the characterization check would then pass by measuring nothing. This
    states the dependency out loud so the coupling is not rediscovered by a silent green run.
    """
    assert pkg.shell_home.__module__ == "phaze.routers.shell"
    assert pkg.shell_stage.__module__ == "phaze.routers.shell"
