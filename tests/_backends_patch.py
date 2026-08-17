"""Patch helpers for ``phaze.services.backends`` now that it is a PACKAGE (phaze-dr9df).

Before the decomposition, ``monkeypatch.setattr("phaze.services.backends.get_settings", ...)`` was a
single rebind that covered every ``get_settings()`` call in the whole 1,543-NLOC module. The split
did not change any call, but it did change where each one RESOLVES: ``KueueBackend.dispatch`` now
reads ``phaze.services.backends.kueue.get_settings``, ``hold_awaiting_cloud`` reads
``phaze.services.backends.admission.get_settings``, and so on. Rebinding the re-export facade's
attribute rebinds none of them.

:func:`patch_backends_get_settings` restores the old ONE-CALL semantics by rebinding the name in
every submodule of the package that binds it. It enumerates the submodules rather than listing them,
so a future submodule that starts reading settings is covered automatically instead of silently
escaping a hand-maintained list -- which would show up as a test that passes against the real
``get_settings()`` while believing it patched a stub.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import TYPE_CHECKING, Any

import phaze.services.backends as backends_pkg


if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest


def patch_backends_attr(monkeypatch: pytest.MonkeyPatch, name: str, value: Any) -> None:
    """Rebind ``name`` to ``value`` in EVERY ``phaze.services.backends`` submodule that binds it.

    The package-aware replacement for a pre-split
    ``monkeypatch.setattr("phaze.services.backends.<name>", value)``, for the case where the caller
    genuinely means "wherever the backends seam calls this, call mine instead" rather than "this one
    submodule's call site". Prefer a direct ``monkeypatch.setattr(<submodule>, ...)`` when the test is
    about ONE call site -- that is both narrower and more legible about what it is exercising.
    """
    patched = 0
    for info in pkgutil.iter_modules(backends_pkg.__path__, prefix=f"{backends_pkg.__name__}."):
        module = importlib.import_module(info.name)
        if hasattr(module, name):
            monkeypatch.setattr(module, name, value)
            patched += 1
    # Fail loud rather than silently no-op: a rename/move that leaves this helper patching nothing
    # would otherwise turn every caller into a test running against the REAL dependency, green and blind.
    assert patched, f"no phaze.services.backends submodule binds {name!r} -- the patch target is stale"


def patch_backends_get_settings(monkeypatch: pytest.MonkeyPatch, factory: Callable[[], Any]) -> None:
    """Rebind ``get_settings`` to ``factory`` across the whole ``phaze.services.backends`` package.

    The package-aware replacement for the pre-split
    ``monkeypatch.setattr("phaze.services.backends.get_settings", factory)``. Six submodules read
    settings (admission / local / compute_agent / kueue / lane_detail / lane_snapshot) and a drain or
    reconcile tick can cross several of them in one call, so "all of them" is the faithful translation.
    """
    patch_backends_attr(monkeypatch, "get_settings", factory)
