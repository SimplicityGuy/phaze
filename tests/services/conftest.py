"""Test harness for the top-level ``services/`` sidecars (audfprint, panako).

The fingerprint sidecars live OUTSIDE ``src/phaze`` -- each is its own standalone uv
project under ``services/<name>/`` with its own ``pyproject.toml`` and ``app.py``. They
are therefore NOT importable by dotted name from the phaze test environment, which is
exactly why ``services/audfprint/app.py`` shipped with zero regression tests (the direct
cause of the P0 audfprint parser bug, phaze-uciu.4).

This conftest wires those modules into the existing ``tests`` testpath by loading each
``app.py`` from its file path under a UNIQUE module name -- both sidecars name their
module ``app``, so a naive ``import app`` would collide. Loading by ``spec_from_file_location``
with a distinct synthetic name keeps them isolated while letting coverage attribute the
executed lines back to their real ``services/<name>/app.py`` path.

No pyproject change is needed: ``tests/services`` sits inside the existing
``testpaths = ["tests"]`` and is collected by a bare ``uv run pytest`` / ``uv run pytest
tests/services``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from typing import TYPE_CHECKING

import pytest


if TYPE_CHECKING:
    from types import ModuleType


SERVICES_DIR = Path(__file__).resolve().parents[2] / "services"


def load_service_module(service: str, unique_name: str) -> ModuleType:
    """Load ``services/<service>/app.py`` under ``unique_name`` (collision-free)."""
    path = SERVICES_DIR / service / "app.py"
    spec = importlib.util.spec_from_file_location(unique_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        msg = f"could not build import spec for {path}"
        raise ImportError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def audfprint_app() -> ModuleType:
    """The ``services/audfprint/app.py`` module, freshly loaded per test."""
    return load_service_module("audfprint", "phaze_test_services_audfprint_app")
