"""Smoke tests: the top-level ``services/`` sidecars import cleanly under pytest.

This is the harness-proving test for phaze-uciu.1 -- before this, ``services/`` was
outside the pytest testpaths entirely, so a broken sidecar module could ship without a
single collected test noticing. A trivial import that exercises the FastAPI app object
is enough to prove the harness is wired; the behavioural regression coverage for the
audfprint parser lives in ``test_audfprint_app.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import FastAPI

from tests.services.conftest import load_service_module


if TYPE_CHECKING:
    from types import ModuleType


def test_audfprint_app_imports(audfprint_app: ModuleType) -> None:
    """services/audfprint/app.py imports and exposes a FastAPI app."""
    assert isinstance(audfprint_app.app, FastAPI)
    assert audfprint_app.app.title == "audfprint Service"


def test_panako_app_imports() -> None:
    """services/panako/app.py imports and exposes a FastAPI app."""
    panako_app = load_service_module("panako", "phaze_test_services_panako_app")
    assert isinstance(panako_app.app, FastAPI)
