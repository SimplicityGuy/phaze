"""Guards on the ``routers/pipeline`` PACKAGE facade (phaze-oau1o).

``src/phaze/routers/pipeline.py`` was one 3,233-line module. Splitting it into a package is only
safe because ``__init__.py`` re-exports the whole previous surface onto a single shared ``router``.
Three things can silently undo that, and each gets a test here:

1. **A route path drifts.** Templates address these endpoints by literal URL, so a changed path is a
   severance with no import error and no failing type check -- this repo has shipped exactly that
   bug twice (phaze-u2p8s). ``test_every_route_is_mounted_at_its_frozen_url`` pins all 30
   (method, path) pairs as data.
2. **A submodule name shadows a re-exported name.** ``from .dashboard import dashboard`` rebinds
   the ``dashboard`` ATTRIBUTE of the package from the module to the function, after which
   ``monkeypatch.setattr("phaze.routers.pipeline.dashboard.get_backend_lane_snapshot", ...)``
   raises ``AttributeError`` on a *function*. That is why the dashboard submodule is called
   ``dashboard_stats``. ``test_no_submodule_name_is_shadowed_by_a_re_export`` stops the next one.
3. **``TEMPLATES_DIR`` loses a ``.parent``.** ``_common.py`` sits one directory deeper than the old
   module, so the walk up to ``src/phaze/templates`` needs three hops. Jinja2Templates accepts a
   non-existent directory happily and every render then fails at REQUEST time, far from the cause.
"""

from __future__ import annotations

import pkgutil


# Frozen at the commit that split the module, generated from the pre-split router object. Any edit
# to this list is a URL change and must be justified as one -- never "fixed" to match the code.
FROZEN_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/api/v1/analyze"),
        ("POST", "/api/v1/extract-metadata"),
        ("POST", "/api/v1/proposals/generate"),
        ("GET", "/pipeline/"),
        ("POST", "/pipeline/analyze"),
        ("GET", "/pipeline/analyze-files"),
        ("POST", "/pipeline/analysis-failed/retry"),
        ("POST", "/pipeline/arm-tracklist-drain"),
        ("POST", "/pipeline/backfill-cloud"),
        ("POST", "/pipeline/disarm-tracklist-drain"),
        ("POST", "/pipeline/extract-metadata"),
        ("GET", "/pipeline/files"),
        ("POST", "/pipeline/files/{file_id}/analysis-failed/retry"),
        ("POST", "/pipeline/files/{file_id}/metadata-failed/retry"),
        ("POST", "/pipeline/files/{file_id}/skip/{stage}"),
        ("GET", "/pipeline/files/{file_id}/trace/{stage}"),
        ("GET", "/pipeline/lanes/{backend_id}"),
        ("POST", "/pipeline/match-tracklists"),
        ("POST", "/pipeline/metadata-failed/retry"),
        ("GET", "/pipeline/pending-files"),
        ("POST", "/pipeline/proposals"),
        ("POST", "/pipeline/recover"),
        ("GET", "/pipeline/recover/status"),
        ("POST", "/pipeline/run-tracklist-drain"),
        ("GET", "/pipeline/stats"),
        ("GET", "/pipeline/tracklist-drain-status"),
        ("GET", "/pipeline/tracklist-sets"),
        ("POST", "/pipeline/tracklists/{file_id}/prioritize"),
        ("POST", "/pipeline/tracklists/{file_id}/refresh"),
        ("POST", "/pipeline/tracklists/{file_id}/unprioritize"),
    }
)


def _mounted() -> set[tuple[str, str]]:
    from phaze.routers.pipeline import router

    return {(method, route.path) for route in router.routes for method in route.methods}


def test_every_route_is_mounted_at_its_frozen_url() -> None:
    """All 30 (method, path) pairs still resolve off the ONE shared router, unchanged by the split."""
    mounted = _mounted()
    assert mounted == FROZEN_ROUTES, (
        f"pipeline router URL drift.\n  vanished: {sorted(FROZEN_ROUTES - mounted)}\n  appeared: {sorted(mounted - FROZEN_ROUTES)}"
    )


def test_the_split_did_not_fragment_the_router() -> None:
    """Every submodule decorates the SAME ``APIRouter``; none creates a second one.

    A submodule that built its own ``APIRouter`` would import cleanly, type-check cleanly, and mount
    nothing -- its routes would simply be absent from the app.
    """
    import phaze.routers.pipeline as pkg
    from phaze.routers.pipeline._common import router as shared

    seen = 0
    for info in pkgutil.iter_modules(pkg.__path__):
        mod = __import__(f"phaze.routers.pipeline.{info.name}", fromlist=["_"])
        if (own := getattr(mod, "router", None)) is not None:
            assert own is shared, f"{info.name} defines its own APIRouter; its routes would never mount"
            seen += 1
    assert seen >= 10, f"expected every route submodule to bind the shared router, only {seen} did"


def test_no_submodule_name_is_shadowed_by_a_re_export() -> None:
    """No submodule shares a name with anything ``__init__`` re-exports.

    When it does, the ``from`` import wins and the package attribute stops being the module -- so a
    patch target naming that submodule silently resolves to a function and raises ``AttributeError``
    (or worse, patches the wrong object). This is why the dashboard submodule is ``dashboard_stats``.
    """
    import phaze.routers.pipeline as pkg

    submodules = {info.name for info in pkgutil.iter_modules(pkg.__path__)}
    collisions = submodules & set(pkg.__all__)
    assert not collisions, f"submodule names shadowed by a re-export (rename the submodule): {sorted(collisions)}"

    for name in submodules:
        assert type(getattr(pkg, name)).__name__ == "module", (
            f"phaze.routers.pipeline.{name} is not the submodule -- patch targets naming it will misresolve"
        )


def test_templates_dir_resolves_to_the_real_template_root() -> None:
    """``TEMPLATES_DIR`` survived moving one directory deeper (three ``.parent`` hops, not two)."""
    from phaze.routers.pipeline import TEMPLATES_DIR

    assert TEMPLATES_DIR.is_dir(), f"{TEMPLATES_DIR} does not exist -- a missing .parent hop; renders would fail at request time"
    assert (TEMPLATES_DIR / "pipeline" / "partials" / "_stage_pill.html").is_file(), (
        f"{TEMPLATES_DIR} exists but is not the template root this package renders from"
    )


def test_the_facade_still_exports_the_names_other_modules_import() -> None:
    """The cross-module imports that would break silently at RUNTIME, not import time."""
    import phaze.routers.pipeline as pkg

    # routers/main.py, routers/shell.py -- the three names imported from this package elsewhere.
    for name in ("router", "FILES_SORT", "build_dashboard_context"):
        assert hasattr(pkg, name), f"phaze.routers.pipeline.{name} is gone; an out-of-package importer depends on it"

    missing = [n for n in pkg.__all__ if not hasattr(pkg, n)]
    assert not missing, f"__all__ advertises names the package does not bind: {missing}"
