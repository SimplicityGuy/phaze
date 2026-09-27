"""Guard: no production module reintroduces the retired ``phaze.config.settings`` singleton
(phaze-mvq8z.2).

``phaze.config`` used to carry TWO independently-constructed settings objects: the import-time
module-level ``settings`` (a ``ControlSettings`` instance built once when ``phaze.config`` first
loaded, regardless of ``PHAZE_ROLE``) and the ``@lru_cache``d ``get_settings()``. A runtime reload
that rebuilt one but not the other would silently diverge from it -- the reason this molecule
(phaze-mvq8z) exists at all: hot-reload has to swap exactly one process-wide settings object, not
guess which of two a given call site happens to read. phaze-mvq8z.2 deleted the singleton and its
``_build_default_settings()`` constructor; every one of its ~15 production importers (routers,
tasks, ``database.py``, two import-time schema consumers, ``services/agent_bootstrap.py``,
``main.py``) moved to ``get_settings()``, cast to ``ControlSettings`` at each call site that reads a
control-only field -- the same idiom ``services/backends/*`` and 20+ other files already used.

mypy + ``import phaze`` are the PRIMARY guard: ``from phaze.config import settings`` fails to
resolve the moment ``settings`` is gone, so a reintroduction breaks loudly before this test ever
runs. This AST-based scan is the SECONDARY backstop -- the same shape as
``tests/shared/test_no_filestate_guard.py``'s D-08 guard -- catching a reintroduction even via a
shim or re-export that would make the import resolve again without actually fixing the underlying
two-singleton divergence.
"""

from __future__ import annotations

import ast
from pathlib import Path
import tempfile


_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "phaze"


def _iter_src_files() -> list[Path]:
    return sorted(_SRC_ROOT.rglob("*.py"))


def _imports_config_settings(path: Path) -> bool:
    """True if ``path`` has an executable ``from phaze.config import settings`` (any alias)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "phaze.config":
            for alias in node.names:
                if alias.name == "settings":
                    return True
    return False


def test_no_module_level_settings_import_in_src() -> None:
    """No src/phaze module imports the retired ``phaze.config.settings`` singleton (phaze-mvq8z.2)."""
    py_files = _iter_src_files()
    # Vacuous-glob assert: a silent empty glob must not pass (guard must actually scan real files).
    assert py_files, f"guard scanned no src/phaze files under {_SRC_ROOT}"

    violations = [str(p) for p in py_files if _imports_config_settings(p)]
    assert not violations, (
        "`from phaze.config import settings` reintroduced -- phaze-mvq8z.2 removed the module-level "
        "singleton because it could diverge from get_settings()'s cached instance under a runtime "
        'reload. Use `get_settings()` instead, `cast("ControlSettings", get_settings())` at the call '
        "site when a control-only field is needed:\n" + "\n".join(violations)
    )


def test_phaze_config_module_has_no_settings_attribute() -> None:
    """``phaze.config`` itself carries no ``settings`` name -- the singleton is gone, not just unimported."""
    import phaze.config as config_module

    assert not hasattr(config_module, "settings"), (
        "phaze.config.settings reintroduced -- this was the divergence-prone singleton phaze-mvq8z.2 removed"
    )
    assert not hasattr(config_module, "_build_default_settings"), (
        "phaze.config._build_default_settings reintroduced -- it existed only to build the removed singleton"
    )


def test_guard_flags_a_planted_reintroduction_and_not_lookalikes() -> None:
    """Proves the scan is not a vacuous no-op: a planted import matches; real lookalikes do not."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write("from phaze.config import settings\n\nx = settings.log_level\n")
        planted = Path(f.name)
    try:
        assert _imports_config_settings(planted)
    finally:
        planted.unlink()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write("from phaze.config import settings as cfg\n")
        planted_aliased = Path(f.name)
    try:
        assert _imports_config_settings(planted_aliased)
    finally:
        planted_aliased.unlink()

    # Negative lookalikes -- every other real name phaze.config exports must NOT match.
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write("from phaze.config import get_settings, AgentSettings, ControlSettings, Settings, Role\n")
        clean = Path(f.name)
    try:
        assert not _imports_config_settings(clean)
    finally:
        clean.unlink()
